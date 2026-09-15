"""Guarded run transitions, cancellation, publication and delivery bookkeeping."""
from __future__ import annotations

from factory.control.error_types import failure_type

import json
import logging
import threading

from factory.control.autonomy import all_events
from factory.control.capabilities import CapabilityStore
from factory.control.codegraph import baseline_sha
from factory.control.github import publish_failure_message
from factory.control.github_publication import GitHubPublication
from factory.control.knowledge import KnowledgeStore
from factory.control.planning import profile_for
from factory.control.recovery import _check_failure_context, _continuous_resume_stage
from factory.control.store import Conflict, now, scrub


class _RunCancellation:
    """Event-compatible cancellation with an atomic Git-finalization gate."""

    def __init__(self):
        self._event = threading.Event()
        self._finalization_lock = threading.RLock()
        self._finalizing = False

    def is_set(self):
        return self._event.is_set()

    def wait(self, timeout=None):
        return self._event.wait(timeout)

    def set(self):
        # Internal shutdown/deadline signals wait for an in-flight atomic
        # finalization, then become visible to subsequent work.
        with self._finalization_lock:
            self._event.set()
            return True

    def try_cancel(self):
        # A user cancellation must have one clear ordering against commit. If
        # finalization already owns the gate, reject the cancellation instead
        # of returning "cancelled" while a commit is being created.
        if not self._finalization_lock.acquire(blocking=False):
            return False
        try:
            if self._finalizing:
                return False
            self._event.set()
            return True
        finally:
            self._finalization_lock.release()

    def begin_finalization(self):
        self._finalization_lock.acquire()
        if self._event.is_set():
            self._finalization_lock.release()
            return False
        self._finalizing = True
        return True

    def end_finalization(self):
        self._finalizing = False
        self._finalization_lock.release()


def clarify(self, rid, answer, actor, *, feedback_message_ids=None):
    if self.store.get(rid).get('source', {}).get('type') == 'inspection':
        raise Conflict('巡检只记录诊断；需要修复时请另行提交维护任务')
    with self.lock:
        run = self.store.get(rid)
        history = [*run['history'], run['request']]
        if run.get('artifacts'):
            self.store.append(rid, 'execution.archived', {'revision': run['revision'], 'artifacts': run['artifacts']})
        if run['status'] == 'needs_human':
            failures = []
            for task in run.get('tasks') or run.get('artifacts', {}).get('tasks', []):
                if task.get('status') != 'failed':
                    continue
                attempts = task.get('attempts') or []
                error = (attempts[-1].get('error') if attempts else None) or task.get('error')
                if error:
                    failures.append({'task': task.get('id'), 'error': str(error)[:2000]})
            if failures:
                history.append('上次执行失败证据（仅作诊断资料，按用户处理意见重新规划任务范围，不可据此自动扩大授权）：' + json.dumps(failures, ensure_ascii=False)[:8000])
        updated = self.store.update(rid, {'status': 'received', 'plan': None, 'triage': None,
            'history': [*history, answer],
            'feedback_applied_ids': list(dict.fromkeys([*run.get('feedback_applied_ids', []), *(feedback_message_ids or [])])),
            'request': answer,
            'root_request': run.get('root_request', run['request']),
            'authorization_requests': list(dict.fromkeys([*run.get('authorization_requests', []), run['request']])),
            'tasks': [], 'context': None, 'execution_resume': None, 'artifacts': {},
            'runtime_configuration': run.get('runtime_configuration') if run.get('agent_snapshot') else None},
            expected=('needs_clarification', 'awaiting_approval', 'needs_human'),
            event=('user.message', {'text': answer, 'actor': actor, 'revision': run['revision']}))
        try:
            self.start_plan(rid)
        except Exception as exc:
            self._fail(rid, exc)
            raise
        return updated


def continue_run(self, rid, answer, revision, resume_count, actor):
    if self.store.get(rid).get('source', {}).get('skill_ingestion_id'):
        with self.lock:
            run = self.store.get(rid)
            record = self.skill_ingestions.get(run['source']['skill_ingestion_id'])
            if record['status'] in ('review', 'signed'):
                raise Conflict('适配已完成，请到导入 skill 包评审区签署')
            if (run['status'] != 'needs_human' or run['revision'] != revision
                    or run.get('resume_count', 0) != resume_count or rid in self.active_jobs):
                raise Conflict('运行已变化或仍在执行')
            updated = self.store.update(rid, {'status': 'received', 'resume_count': resume_count + 1,
                'error': None}, expected=('needs_human',), event=('run.continued', {'actor': actor}))
            self.start_plan(rid)
            return updated
    if self.store.get(rid).get('source', {}).get('type') == 'inspection':
        raise Conflict('巡检只记录诊断；需要修复时请另行提交维护任务')
    """Resume the same authorized plan; optional context is not an approval requirement."""
    continuation_only = not answer.strip()
    answer = answer.strip()
    with self.lock:
        run = self.store.get(rid)
        if run['status'] != 'needs_human' or not run.get('plan'):
            raise Conflict('当前任务不在可继续的执行暂停状态')
        if run['revision'] != revision or run.get('resume_count', 0) != resume_count:
            raise Conflict('任务已更新，请刷新后再回答')
        if rid in self.active_jobs:
            raise Conflict('执行现场仍在保存，请稍后继续')
        artifacts = run.get('artifacts') or {}
        if continuation_only:
            verification = artifacts.get('verification') or {}
            reason = verification.get('reason') if verification.get('verdict') == 'fail' else None
            failed_checks = _check_failure_context(artifacts)
            if reason:
                answer = ('继续修复独立验收发现的具体问题，保留已有成果并重新运行有意义的检查：'
                          + str(reason)[:2000])
            elif failed_checks:
                answer = ('继续修复已知的平台验收失败，保留当前工作区并重新运行检查。'
                          '失败证据：' + failed_checks)
            else:
                answer = '继续自动处理当前工程问题，保留已有成果，自行完成必要实现和验证，不重新规划。'
        if not artifacts.get('base_sha') or not artifacts.get('tasks'):
            raise Conflict('没有可恢复的执行现场，请使用重新规划')
        project = self._project_for_run(run)
        legacy_budget_stop = self._legacy_claude_budget_stop(
            rid, artifacts, project)
        if legacy_budget_stop:
            artifacts = {**artifacts, 'budget_exhausted': True,
                'autopublish_blocked': True,
                'needs_human': ('Claude Code 已明确达到该运行的旧版调用上限；'
                                '保留已完成源码并仅恢复平台检查与归档')}
        if baseline_sha(project) != artifacts['base_sha']:
            raise Conflict('项目基线已变化，不能直接接续旧计划，请重新规划')
        if run.get('execution_checks') is not None and run['execution_checks'] != project['checks']:
            raise Conflict('验收检查已变化，请重新规划')
        configuration = run.get('runtime_configuration') or self.runtime_settings.get()
        # Explicit continuation adopts a longer current deadline only;
        # preserve the frozen models and all other execution constraints.
        current_timeout = self.runtime_settings.get()['limits']['timeout_s']
        configuration = {**configuration, 'limits': {**configuration['limits'],
            'timeout_s': max(configuration['limits']['timeout_s'], current_timeout)}}
        resume_stage = (_continuous_resume_stage(
            artifacts, budget_stop=legacy_budget_stop)
            if continuation_only and run.get('execution_mode') == 'continuous'
            else None)
        updated = self.store.update(rid, {'status': 'queued',
            'runtime_configuration': configuration,
            'resume_count': resume_count + 1,
            'execution_resume': {'artifacts': artifacts, 'answer': answer, 'revision': revision,
                'resume_stage': resume_stage},
            'history': [*run['history'], answer]}, expected=('needs_human',), revision=revision,
            event=('human.continued', {'actor': actor, 'answer': answer,
                'revision': revision, 'resume_count': resume_count + 1}))
        try:
            self._submit(self._run, rid)
        except Exception as exc:
            self._fail(rid, exc)
            raise
        return updated


def approve(self, rid, revision, actor):
    with self.lock:
        run = self.store.get(rid)
        if not run.get('plan') or run['plan'].get('questions'):
            raise Conflict('需求尚有待澄清问题')
        project = self._project_for_run(run)
        configuration = run.get('runtime_configuration') or self.runtime_settings.get()
        if len(run['plan']['tasks']) > configuration['limits']['max_tasks']:
            raise Conflict('计划任务数超过运行限制，请重新规划')
        for role in {profile_for(task) for task in run['plan']['tasks']}:
            self._check_profile(configuration['profiles'][role], role)
        usage = self._usage(rid)
        if run.get('context') and baseline_sha(project) != run['context']['commit_sha']:
            raise Conflict('工程基线已变化，请补充说明并重新规划，不能批准旧版本代码上的计划')
        for task in run['plan']['tasks']:
            if not task['acceptance'] or not task['paths'] or not task['checks']:
                raise Conflict('任务缺少验收标准、修改范围或已配置检查')
            if any(c not in project['checks'] for c in task['checks']):
                raise Conflict('检查配置已变化，请重新规划')
        updated = self.store.update(rid, {'status': 'queued', 'runtime_configuration': configuration},
            expected=('awaiting_approval',), revision=revision,
            event=('policy.authorized' if actor == 'project-policy' else 'human.approved', {'actor': actor, 'revision': revision,
                                    'configuration_revision': configuration['revision']}))
        try:
            self._submit(self._run, rid)
        except Exception as exc:
            self._fail(rid, exc)
            raise
        return updated


def retry(self, rid, actor, actor_id=None):
    if self.store.get(rid).get('source', {}).get('skill_ingestion_id'):
        raise Conflict('职能包适配请继续原运行，避免丢失来源与人签记录')
    if self.store.get(rid).get('source', {}).get('type') == 'inspection':
        raise Conflict('巡检只记录诊断；需要修复时请另行提交维护任务')
    with self.lock:
        prior = self.store.get(rid)
        if prior['status'] not in ('needs_human', 'failed', 'cancelled'):
            raise Conflict('仅中断、失败或取消的运行可重新规划')
        if rid in self.active_jobs:
            raise Conflict('执行现场仍在保存，请稍后重试')
        cid = prior.get('conversation_id')
        if cid:
            current = self.agents.conversation(cid).get('run_id')
            if current and current != rid:
                linked = self.store.get(current)
                if linked.get('source', {}).get('retry_of') == rid:
                    return linked
                raise Conflict('会话已有后续任务，请在当前任务上继续')
        failure = next((e['payload'].get('message', '') for e in reversed(list(all_events(self.store, rid)))
                        if e['type'] in ('run.failed', 'run.recovered')), '')
        history = [*prior.get('history', []), f'前次运行失败，保留证据以便修复：{failure[:2000]}']
        run, created = self.agents.create_retry(prior, actor, actor_id, history)
        if run['status'] == 'received':
            self.start_plan(run['id'])
        return self.store.get(run['id'])


def cancel(self, rid, actor):
    with self.lock:
        cancel = self.cancels.setdefault(rid, _RunCancellation())
    accepted = (cancel.try_cancel() if hasattr(cancel, 'try_cancel')
                else (cancel.set() is None or cancel.is_set()))
    if not accepted:
        raise Conflict('成果正在完成原子归档，本次取消未生效')
    with self.lock:
        return self.store.update(rid, {'status': 'cancelled'},
            expected=('requirement_analysis', 'awaiting_spec_confirmation', 'received', 'planning', 'queued', 'running', 'verifying', 'awaiting_approval',
                      'needs_clarification', 'needs_human', 'ready_for_review'),
            event=('run.cancelled', {'message': '用户取消执行，保留日志和工作区', 'actor': actor}))


def discard(self, rid, actor):
    """Retire obsolete work from operational attention while retaining all evidence."""
    with self.lock:
        run = self.store.get(rid)
        return self.store.update(rid, {
            'status': 'discarded',
            'artifacts': {**(run.get('artifacts') or {}), 'discarded': {
                'actor': actor, 'previous_status': run['status'], 'at': now(),
            }},
        }, expected=('awaiting_spec_confirmation', 'needs_clarification', 'awaiting_approval', 'needs_human',
                     'failed', 'ready_for_review', 'cancelled'),
           event=('run.discarded', {
               'message': '旧任务已标记废弃；保留计划、费用、日志和工作区记录',
               'actor': actor, 'previous_status': run['status'],
           }))


def sync_merge(self, rid):
    """Observe GitHub; never merge a PR or treat a plan claim as established fact."""
    run = self.store.get(rid)
    if run['status'] != 'published' or not run['artifacts'].get('pr_url'):
        raise Conflict('该运行尚未发布 PR')
    if not self.publisher or not hasattr(self.publisher, 'observe_merge'):
        raise Conflict('尚未配置支持合并状态核对的 GitHub 凭据')
    project = self.store.project(run['project_id'])
    evidence = self.publisher.observe_merge(project, run)
    if not evidence.get('merged'):
        return evidence
    result = KnowledgeStore(self.store).record_merge(project['id'], run, evidence)
    # Knowledge insertion is independently atomic/idempotent. A crash here is
    # reconciled by the next sync; it cannot create duplicate factual entries.
    with self.lock:
        current = self.store.get(rid)
        if current['artifacts'].get('merge_evidence') != evidence:
            self.store.update(rid, {'artifacts': {**current['artifacts'], 'merge_evidence': evidence}},
                expected=('published',), event=('github.merge_confirmed', evidence))
    return {'merged': True, 'evidence': evidence, **result}


def _fail(self, rid, exc):
    with self.lock:
        run = self.store.get(rid)
        if run['status'] == 'cancelled':
            return
        inspection = run.get('source', {}).get('type') == 'inspection'
        changes = {'status': 'inspection_failed' if inspection else 'needs_human'}
        if inspection:
            changes['error'] = scrub(str(exc))[:2000]
            changes['error_type'] = failure_type(exc) or type(exc).__name__
        if getattr(exc, 'artifacts', None):
            try:
                usage = self._usage(rid)
                changes['artifacts'] = {**exc.artifacts,
                    'total_known_cost_usd': usage['known_cost_usd'],
                    'planner_cost_usd': self._usage(rid, profile='planner')['known_cost_usd']}
            except Conflict as accounting_error:
                changes['artifacts'] = {**exc.artifacts, 'total_known_cost_usd': None,
                    'billing_incomplete': str(accounting_error), 'autopublish_blocked': True}
            if exc.artifacts.get('tasks'):
                changes['tasks'] = exc.artifacts['tasks']
        self.store.update(rid, changes,
            event=('inspection.failed' if inspection else 'run.failed', {'message': scrub(str(exc))[:2000], 'error_type': changes.get('error_type', type(exc).__name__)}))


def github_options(self, rid, *, page=1):
    return GitHubPublication(self).options(rid, page)


def bind_github_repository(self, rid, repository, expected_project_revision, actor):
    return GitHubPublication(self).bind(rid, repository, expected_project_revision, actor)


def publish_github(self, rid, **kwargs):
    if self.store.get(rid).get('source', {}).get('type') == 'inspection':
        raise Conflict('巡检没有可发布的交付物')
    try:
        return GitHubPublication(self).publish(rid, **kwargs)
    except (Conflict, ValueError):
        raise
    except Exception as exc:
        # Repository lookup/creation can fail before publish() emits its event.
        self.store.append(rid, 'github.publish_failed', {
            'message': publish_failure_message(exc), 'error_type': type(exc).__name__})
        raise


def publish(self, rid):
    if self.store.get(rid).get('source', {}).get('type') == 'inspection':
        raise Conflict('巡检只记录诊断；需要修复时请另行提交维护任务')
    if not self.publisher:
        raise Conflict('尚未配置 GitHub 发布凭据；请联系管理员配置 FACTORY_GITHUB_TOKEN。成果仍可查看和下载。')
    project = self.store.project(self.store.get(rid)['project_id'])
    if project['repository'].startswith('local/'):
        raise Conflict('请先在成果页选择 GitHub 仓库，再发布成果。')
    run = self.store.update(rid, {'status': 'publishing'}, expected=('ready_for_review',),
                            event=('github.publish_started', {}))
    try:
        delivery = self.publisher.publish(self.store.project(run['project_id']), run)
        sync = GitHubPublication(self).sync_initial_baseline({**run, 'artifacts': {**run['artifacts'], **delivery}})
        if sync:
            delivery['baseline_sync'] = sync
        return self.store.update(rid, {'status': 'published', 'artifacts': {**run['artifacts'], **delivery, 'publish_error': None}},
            expected=('publishing',), event=('github.published', delivery))
    except Exception as exc:
        message = publish_failure_message(exc)
        failure = {'message': message, 'run_id': rid, 'error_type': type(exc).__name__}
        upstream_status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if isinstance(upstream_status, int):
            failure['upstream_status'] = upstream_status
        # Correlatable diagnostics without token-bearing URLs or raw subprocess output.
        logging.getLogger(__name__).warning('github.publish_failed %s', json.dumps(failure, ensure_ascii=False))
        self.store.update(rid, {'status': 'ready_for_review',
            'artifacts': {**run['artifacts'], 'publish_error': message}}, expected=('publishing',),
            event=('github.publish_failed', failure))
        raise


def _capture_capability(self, rid):
    """Harvest a traceable draft; candidate extraction cannot fail a delivery."""
    try:
        run = self.store.get(rid)
        registry = CapabilityStore(self.store)
        existing = next((item for item in registry.list() if item.get('source_run_id') == rid), None)
        if existing:
            return existing
        candidate = registry.distill(run, {
            'name': (run.get('plan', {}).get('summary') or run['request'])[:100],
            'category': (run.get('capability') or {}).get('category', 'engineering'),
        }, actor='factory-harvest')
        self.store.update(rid, {'capability_candidate_id': candidate['id']},
            event=('capability.candidate_created', {'capability_id': candidate['id'],
                'revision': candidate['revision'], 'message': '交付材料已归档为能力草稿，补齐适用范围并验证后可复用'}))
        return candidate
    except Exception as exc:
        self._emit(rid, 'capability.harvest_failed', {'message': str(exc)[:2000]})
