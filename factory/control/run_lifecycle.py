"""Guarded run transitions, cancellation, publication and delivery bookkeeping."""
from __future__ import annotations

from factory.control.error_types import failure_type

import hashlib
import json
import logging
import threading
import time
import uuid

from factory.control.autonomy import all_events
from factory.control.capabilities import CapabilityStore
from factory.control.codegraph import baseline_sha
from factory.control import effective_contract
from factory.control.github import publish_failure_message
from factory.control.github_publication import GitHubPublication
from factory.control.knowledge import KnowledgeStore
from factory.control.planning import profile_for
from factory.control.recovery import _check_failure_context, _continuous_resume_stage
from factory.control.store import Conflict, now, scrub


def _collect_pending_followups(svc, rid):
    """Read-only: return unconsumed followup.pending payloads, or empty list.

    Must be called under svc.lock.  Does NOT write followup.applied events --
    the caller must call _mark_followups_applied after all Conflict checks pass
    so that a failed check never leaves a pending item marked applied without
    its content being merged into an actual execution.
    """
    applied_ids = set()
    for event in svc.store.export_events(rid, kind='followup.applied'):
        applied_ids.add(event['payload'].get('pending_id'))
    pending = []
    for event in svc.store.export_events(rid, kind='followup.pending'):
        p = event['payload']
        if p['id'] not in applied_ids:
            pending.append(p)
    return pending


def _merge_followup_content(answer, pending):
    """Merge collected pending follow-ups into an answer string.

    Returns (merged_answer, had_followups).
    """
    if not pending:
        return answer, False
    followup_text = '\n\n'.join(p['content'] for p in pending)
    merged = (answer + '\n\n' if answer.strip() else '') + '[用户在执行中补充的要求]\n' + followup_text
    return merged, True


def _applied_events(run, pending, *, contract=None):
    """The followup.applied receipts for one consumption, for the same transaction.

    These belong with the change they record: a receipt that lands without the
    revised agreement claims a supplement was honoured when it was not, and a
    revision that lands without its receipt invites a second application of the
    same words. `store.update(..., events=...)` writes both or neither.
    """
    return [('followup.applied', {'pending_id': p['id'], 'run_revision': run['revision'],
                                  **({'effective_revision': contract['revision'],
                                      'effective_digest': contract['digest']} if contract else {})})
            for p in pending]


def _mark_followups_applied(svc, rid, pending):
    """Write followup.applied receipts for a consumption that had no contract change.

    Prefer passing `_applied_events(...)` into the same `store.update`. This
    remains for the paths whose update cannot carry them, and is still written
    under svc.lock immediately after that update.
    """
    if not pending:
        return
    run = svc.store.get(rid)
    with svc.store.connect() as db:
        for kind, payload in _applied_events(run, pending):
            svc.store._event(db, rid, kind, payload)


def _revise_for_followups(svc, rid, run, collected):
    """Decide each supplement: applied into the next agreement, or still waiting.

    Returns {'contract': next agreement or None, 'applied': [pending], 'waiting':
    [{'pending_id', 'reason', 'detail'}]}. A supplement is applied only when the
    analysis actually decided what it does to the specification -- including
    deciding that it changes nothing. Anything else waits: an undecidable business
    conflict belongs to the customer, and an analysis that never ran (model fault,
    cancellation, exhausted budget) has decided nothing at all. Waiting means the
    supplement stays pending, unconsumed and unreceipted, and the caller must not
    hand its words to coding: text nobody has read against the agreement would be
    implemented as if the owner had confirmed it.

    Costs money, so it runs after the caller's cheap conflict checks and outside
    svc.lock; `deadline` bounds the whole step, not each call, so a queue of
    supplements cannot stretch a safe node past the run's own configured limit.
    The caller re-checks the run and commits atomically after re-acquiring the lock.
    """
    log = logging.getLogger(__name__)
    contract = effective_contract.current(run)
    if contract is None or not collected:
        return {'contract': None, 'applied': list(collected), 'waiting': []}
    project = svc._project_for_run(run)
    configuration = run.get('runtime_configuration') or svc.runtime_settings.get()
    deadline = time.monotonic() + max(5, configuration['limits']['timeout_s'])
    revised, applied, waiting = contract, [], []
    for pending in collected:
        try:
            analysis = effective_contract.analyse(svc, rid, run, project, configuration,
                                                  pending['content'], deadline=deadline)
        except Exception as exc:
            if getattr(exc, 'error_type', None) == 'provider':
                # A deployment with no tools-free channel cannot read any supplement
                # against any agreement. That is a configuration state, not a reason
                # to build unread words: the supplement stays pending and the resume
                # is refused with something the operator can act on, so it succeeds
                # once the channel is configured. Marking it applied here was the
                # silent degradation this module exists to prevent -- it looked like
                # a working run while the agreement had not moved at all.
                log.info('scope_change(%s): no tools-free analysis channel: %s', rid, exc)
                svc.store.append(rid, 'contract.analysis_unsupported', {
                    'pending_id': pending['id'], 'error': str(exc)[:500]})
                waiting.append({'pending_id': pending['id'], 'reason': 'analysis_unconfigured',
                                'detail': str(exc)[:500]})
                continue
            # Nobody read this supplement against the agreement, so nothing about it
            # is decided -- least of all that it is safe to build.
            log.info('scope_change(%s): analysis unavailable: %s: %s', rid, type(exc).__name__, exc)
            detail = f'{type(exc).__name__}: {str(exc)[:500]}'
            svc.store.append(rid, 'contract.analysis_skipped', {
                'pending_id': pending['id'], 'error': detail})
            waiting.append({'pending_id': pending['id'], 'reason': 'analysis_unavailable',
                            'detail': detail})
            continue
        if analysis['unresolved']:
            svc.store.append(rid, 'contract.unresolved', {
                'pending_id': pending['id'], 'questions': analysis['unresolved']})
            waiting.append({'pending_id': pending['id'], 'reason': 'unresolved',
                            'detail': '；'.join(analysis['unresolved'])[:500]})
            continue
        applied.append(pending)
        if not any(analysis[k] for k in ('superseded_non_goals', 'superseded_plan_acceptance',
                                        'added_requirements')):
            continue  # Read and decided: it asks for nothing the agreement does not say.
        revised = effective_contract.revise(revised, analysis, message={
            'pending_id': pending['id'], 'content': pending['content'],
            'actor_id': pending.get('actor_id'), 'actor': pending.get('actor'),
            'fingerprint': pending.get('fingerprint')})
    return {'contract': None if revised is contract else revised,
            'applied': applied, 'waiting': waiting}


def _waiting_conflict(waiting):
    """Why a resume is refused while a supplement is still undecided."""
    unresolved = [w for w in waiting if w['reason'] == 'unresolved']
    if unresolved:
        return Conflict('这条补充的业务范围还需要您确认，暂不能据此继续开发：'
                        + unresolved[0]['detail'] + '\n回答后请使用重新规划，或取消该补充。',
                        error_type='contract_unresolved')
    if any(w['reason'] == 'analysis_unconfigured' for w in waiting):
        # Distinct from a transient failure: retrying changes nothing until an
        # administrator configures a tools-free analysis channel. The supplement is
        # kept, so the retry after that configuration lands applies it.
        return Conflict('本部署尚未配置无工具的范围变更分析通道，补充已保留但暂不能进入开发；'
                        '请管理员配置该通道后重试。',
                        error_type='contract_analysis_unconfigured')
    return Conflict('范围变更分析暂不可用，未读懂的补充不会进入开发，请稍后重试。',
                    error_type='contract_analysis_unavailable')


def _immediate_supplement(svc, rid, answer, actor):
    """Register text typed at a safe node as a durable supplement, or return None.

    A supplement typed while the run waits used to take a different road from one
    typed while it ran: straight into the resume answer, never read against the
    agreement, never receipted -- so the owner was told `applied` while the
    effective revision had not moved and coding got words the contract knew
    nothing about. Registering it here puts both on the one road.

    Registration is durable and happens before any dispatch, so a refusal further
    down leaves the words pending for the customer to settle rather than losing
    them. It is idempotent on (actor, content): a replay of the same key -- whose
    receipt was never written, because the first attempt raised -- reuses the
    pending it already registered instead of opening a second one to consume.

    Returns None for a blank answer and for a run with no confirmed specification,
    which keeps legacy and unconfirmed runs on their previous path exactly.
    """
    content = (answer or '').strip()
    if not content or effective_contract.current(svc.store.get(rid)) is None:
        return None
    fingerprint = hashlib.sha256(content.encode()).hexdigest()
    for p in _collect_pending_followups(svc, rid):
        if p.get('fingerprint') == fingerprint and p.get('actor') == actor:
            return p  # Already registered and still unconsumed: reuse it, do not open a second.
    pending = {'id': uuid.uuid4().hex, 'content': content, 'actor_id': None,
               'actor': actor, 'fingerprint': fingerprint, 'created_at': now(),
               'origin': 'safe_node'}
    svc.store.append(rid, 'followup.pending', pending)
    return pending


def _resume_preconditions(svc, rid, revision, resume_count, answer, merged):
    """Every guard a needs_human resume must satisfy, read from the run as it is now.

    Evaluated once before the paid scope analysis and again after re-acquiring the
    lock, so a change that happens while the analyst is thinking -- a cancellation,
    another safe node, a moved baseline, a new revision -- is refused instead of
    being committed on top of a stale reading. The same function is used both
    times on purpose: a re-check that only repeats some of the guards is a hole
    exactly where the timing window is.

    Raises Conflict; returns the derived inputs the commit needs.
    """
    run = svc.store.get(rid)
    if run['status'] != 'needs_human' or not run.get('plan'):
        raise Conflict('当前任务不在可继续的执行暂停状态')
    if run['revision'] != revision or run.get('resume_count', 0) != resume_count:
        raise Conflict('任务已更新，请刷新后再回答')
    if rid in svc.active_jobs:
        raise Conflict('执行现场仍在保存，请稍后继续')
    cancel = svc.cancels.get(rid)
    if cancel is not None and cancel.is_set():
        raise Conflict('任务已取消')
    answer, had_followups = _merge_followup_content(answer, merged)
    continuation_only = not had_followups and not answer.strip()
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
    project = svc._project_for_run(run)
    legacy_budget_stop = svc._legacy_claude_budget_stop(rid, artifacts, project)
    if legacy_budget_stop:
        artifacts = {**artifacts, 'budget_exhausted': True,
            'autopublish_blocked': True,
            'needs_human': ('Claude Code 已明确达到该运行的旧版调用上限；'
                            '保留已完成源码并仅恢复平台检查与归档')}
    if baseline_sha(project) != artifacts['base_sha']:
        raise Conflict('项目基线已变化，不能直接接续旧计划，请重新规划')
    if run.get('execution_checks') is not None and run['execution_checks'] != project['checks']:
        raise Conflict('验收检查已变化，请重新规划')
    configuration = run.get('runtime_configuration') or svc.runtime_settings.get()
    # Explicit continuation adopts a longer current deadline only;
    # preserve the frozen models and all other execution constraints.
    current_timeout = svc.runtime_settings.get()['limits']['timeout_s']
    configuration = {**configuration, 'limits': {**configuration['limits'],
        'timeout_s': max(configuration['limits']['timeout_s'], current_timeout)}}
    resume_stage = (_continuous_resume_stage(artifacts, budget_stop=legacy_budget_stop)
                    if continuation_only and run.get('execution_mode') == 'continuous' else None)
    return {'run': run, 'artifacts': artifacts, 'answer': answer,
            'configuration': configuration, 'resume_stage': resume_stage}


def _expire_unconsumed_followups(svc, rid):
    """Mark unconsumed pending followups as expired when run reaches a terminal
    success state (ready_for_review, published).  Expired means the content was
    never merged into any execution round; the user sees '任务已结束未并入'.

    Must be called under svc.lock (or after the terminal store.update has
    committed).  Writing followup.expired is safe to repeat -- the routes
    deduplicate by pending_id.
    """
    collected = _collect_pending_followups(svc, rid)
    if not collected:
        return
    with svc.store.connect() as db:
        for p in collected:
            svc.store._event(db, rid, 'followup.expired', {
                'pending_id': p['id'], 'reason': 'run_completed'})


def _auto_resume_with_followups(svc, rid):
    """Auto-resume a needs_human run when unconsumed pending followups exist.

    Called after _fail sets the run to needs_human.  Performs the equivalent of
    continue_run with actor='system/auto', but silently returns False if the run
    is not resumable (no plan, no artifacts, cancelled, etc.).

    Returns True if auto-resume was submitted, False otherwise.
    """
    log = logging.getLogger(__name__)
    with svc.lock:
        run = svc.store.get(rid)
        if run['status'] != 'needs_human':
            return False
        collected = _collect_pending_followups(svc, rid)
        if not collected:
            return False
        # Require resumable artifacts (same as continue_run)
        artifacts = run.get('artifacts') or {}
        if not artifacts.get('base_sha') or not artifacts.get('tasks'):
            log.info('auto_resume(%s): no resumable artifacts, skipping', rid)
            return False
        if not run.get('plan'):
            log.info('auto_resume(%s): no plan, skipping', rid)
            return False
        if rid in svc.active_jobs:
            log.info('auto_resume(%s): active job still running, skipping', rid)
            return False
        # Validate baseline
        try:
            from factory.control.codegraph import baseline_sha
            project = svc._project_for_run(run)
            current = baseline_sha(project)
            if current != artifacts['base_sha']:
                log.info('auto_resume(%s): baseline changed (%s != %s), skipping',
                         rid, current[:12], artifacts['base_sha'][:12])
                return False
        except Exception as exc:
            log.warning('auto_resume(%s): baseline check raised %s: %s',
                        rid, type(exc).__name__, exc)
            svc.store.append(rid, 'followup.auto_resume_skipped', {
                'reason': 'baseline_check_exception',
                'error': f'{type(exc).__name__}: {str(exc)[:500]}'})
            return False
        # Check execution_checks consistency
        if run.get('execution_checks') is not None and run['execution_checks'] != project['checks']:
            log.info('auto_resume(%s): checks changed, skipping', rid)
            return False
        configuration = run.get('runtime_configuration') or svc.runtime_settings.get()
        current_timeout = svc.runtime_settings.get()['limits']['timeout_s']
        configuration = {**configuration, 'limits': {**configuration['limits'],
            'timeout_s': max(configuration['limits']['timeout_s'], current_timeout)}}
        resume_count = run.get('resume_count', 0)
        revision = run['revision']
    # Paid scope analysis with the lock released, like the interactive resume: an
    # automatic safe node must not hold every other lifecycle action behind it.
    decision = _revise_for_followups(svc, rid, run, collected)
    with svc.lock:
        run = svc.store.get(rid)
        if (run['status'] != 'needs_human' or run['revision'] != revision
                or run.get('resume_count', 0) != resume_count or rid in svc.active_jobs):
            log.info('auto_resume(%s): run changed during scope analysis, skipping', rid)
            return False
        if decision['waiting']:
            # Nothing automatic may decide an open business question or build words
            # nobody read. The supplement stays pending for the next safe node.
            log.info('auto_resume(%s): %d supplement(s) still undecided, staying paused',
                     rid, len(decision['waiting']))
            return False
        applied = decision['applied']
        pending_ids = [p['id'] for p in applied]
        answer, _ = _merge_followup_content('', applied)
        contract = decision['contract']
        changes = {'status': 'queued',
            'runtime_configuration': configuration,
            'resume_count': resume_count + 1,
            'execution_resume': {'artifacts': artifacts, 'answer': answer, 'revision': revision,
                'effective_revision': effective_contract.revision_of(
                    {**run, **({'effective_contract': contract} if contract else {})})},
            'history': [*run['history'], answer]}
        if contract:
            changes['effective_contract'] = contract
        try:
            updated = svc.store.update(rid, changes, expected=('needs_human',), revision=revision,
                event=('run.auto_resumed', {'actor': 'system/auto', 'answer': answer,
                    'revision': revision, 'resume_count': resume_count + 1,
                    'pending_ids': pending_ids}),
                events=_applied_events(run, applied, contract=contract))
        except Exception as exc:
            log.info('auto_resume(%s): store.update failed: %s', rid, exc)
            return False
    try:
        svc._submit(svc._run, rid)
    except Exception as exc:
        log.warning('auto_resume(%s): _submit failed: %s', rid, exc)
        svc._fail(rid, exc)
        return False
    log.info('auto_resume(%s): submitted with %d pending followups', rid, len(collected))
    return True


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
        collected = _collect_pending_followups(self, rid)
        answer, _ = _merge_followup_content(answer, collected)
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
        _mark_followups_applied(self, rid, collected)
        try:
            self.start_plan(rid)
        except Exception as exc:
            self._fail(rid, exc)
            raise
        return updated


def continue_run(self, rid, answer, revision, resume_count, actor):
    # Local import breaks the budget_resume -> requirement analysis/lifecycle cycle.
    from factory.control import budget_resume, requirement_analysis
    current = self.store.get(rid)
    if requirement_analysis.required(current) and not current.get('plan'):
        return budget_resume.resume(self, rid, revision, resume_count, actor)
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
    answer = answer.strip()
    with self.lock:
        collected = _collect_pending_followups(self, rid)
        # Cheap guards first: a run that cannot resume at all should not be charged
        # for a scope analysis. Registration still precedes every dispatch below.
        state = _resume_preconditions(self, rid, revision, resume_count, answer, collected)
        immediate = _immediate_supplement(self, rid, answer, actor)
        supplements = list({p['id']: p for p in [*collected, *([immediate] if immediate else [])]}.values())
        # The text is carried by its own pending now, so it is not merged twice.
        merge_answer = '' if immediate else answer
    # A safe point is where the agreement may change: the round that follows reads
    # the revised one, and the receipt for the supplements that revised it is
    # written by the same transaction. The analysis costs money and calls a model,
    # so it runs with the lock released -- holding it would freeze cancel, follow-up
    # and every other lifecycle action behind a call that can take minutes.
    decision = _revise_for_followups(self, rid, state['run'], supplements)
    with self.lock:
        # Re-read under the lock: anything decided above is only usable if the run
        # is still the one it was decided for.
        state = _resume_preconditions(self, rid, revision, resume_count, merge_answer,
                                      decision['applied'])
        if decision['waiting']:
            # Words nobody read against the agreement do not reach coding, and the
            # supplement stays pending for the customer to settle.
            raise _waiting_conflict(decision['waiting'])
        run, artifacts, contract = state['run'], state['artifacts'], decision['contract']
        changes = {'status': 'queued',
            'runtime_configuration': state['configuration'],
            'resume_count': resume_count + 1,
            'execution_resume': {'artifacts': artifacts, 'answer': state['answer'],
                'revision': revision, 'resume_stage': state['resume_stage'],
                'effective_revision': effective_contract.revision_of(
                    {**run, **({'effective_contract': contract} if contract else {})})},
            'history': [*run['history'], state['answer']]}
        if contract:
            changes['effective_contract'] = contract
        updated = self.store.update(rid, changes, expected=('needs_human',), revision=revision,
            event=('human.continued', {'actor': actor, 'answer': state['answer'],
                'revision': revision, 'resume_count': resume_count + 1}),
            events=_applied_events(run, decision['applied'], contract=contract))
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
