"""Service facade: initialization, durable scheduling, maintenance jobs and policy coordination."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import hashlib
import logging
import os
import subprocess
import threading
import time
import uuid

from factory.control import requirement_analysis, recovery, run_billing, run_execution, run_lifecycle, verification
from factory.control.agent_evolution import EvolutionStore
from factory.control.agent_manifests import ManifestStore
from factory.control.skill_ingestion_runs import IngestionStore
from factory.control.agents import AgentStore
from factory.control.autonomy import DurableQueue, PolicyStore, policy_decision
from factory.control.codegraph import baseline_sha
from factory.control.execution import execute_plan
from factory.control.deploy_targets import TargetStore
from factory.control.remote_targets import RemoteTargets
from factory.control.inspections import InspectionStore, inspect_run
from factory.control.modules import ModuleStore
from factory.control.mounts import MountedRunner
from factory.control.operation_presets import OperationStore
from factory.control.operations_automation import OperationsAutomation
from factory.control.planning import profile_for, triage as fresh_triage
from factory.control.project_assistants import ProjectAssistants
from factory.control.providers import ProviderCancelled, SDKRunner
from factory.control.recovery import _check_failure_context, _continuous_resume_stage, _failed_platform_checks, _has_successful_command_evidence
from factory.control.run_billing import _verification_reserve_usd
from factory.control.run_lifecycle import _RunCancellation
from factory.control.runtime import RuntimeSettings, profile_blockers
from factory.control.store import Conflict, Store, now, scrub
from factory.control.verification import _AGENT_FEEDBACK_PREFIXES, _VERIFIER_CONTRACT_MAX_CHARS, _contract_excerpt, _verifier_request_contract


def configured_profiles():
    profiles = {}
    for role in ('planner', 'cheap', 'standard', 'strong'):
        profiles[role] = {
            'provider': os.getenv(f'FACTORY_{role.upper()}_PROVIDER', 'codex'),
            'model': os.getenv(f'FACTORY_{role.upper()}_MODEL', ''),
        }
    return profiles


class Service:
    def __init__(self, store: Store, *, runner=None, publisher=None, profiles=None,
                 execute=None, continuous_execute=None, timeout_s=14400, max_parallel=2):
        self.store = store
        self.runner = runner or SDKRunner()
        self.governance = None
        self.publisher = publisher
        self.runtime_settings = RuntimeSettings(store, profiles=profiles or configured_profiles(),
            limits={'timeout_s': timeout_s, 'max_parallel': max_parallel})
        self.profiles = self.runtime_settings.get()['profiles']
        self.check_runtime = runner is None or isinstance(runner, SDKRunner)
        self.execute = execute or execute_plan
        self.continuous_execute = continuous_execute or execute
        self.timeout_s = timeout_s
        self.max_parallel = max_parallel
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='factory-control')
        self.cancels = {}
        self.lock = threading.RLock()
        self.futures = set()
        self.queue = DurableQueue(store)
        self.policies = PolicyStore(store)
        self.active_jobs = {}
        self.maintenance_jobs = {}
        self.maintenance_cancels = {}
        with self.store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS maintenance_jobs(
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, actor_id INTEGER NOT NULL,
                status TEXT NOT NULL, result TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS maintenance_jobs_conversation ON maintenance_jobs(conversation_id,status);
            """)
            db.execute("UPDATE maintenance_jobs SET status='interrupted',error='服务重启时没有收到回执',updated_at=? WHERE status IN ('pending','running','cancel_requested')", (now(),))
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.scheduler = None
        self.agents = AgentStore(store)
        self.agent_manifests = ManifestStore(store)
        self.skill_ingestions = IngestionStore(store)
        self.agent_manifests.migrate_all()
        self.operations = OperationStore(store)
        self._inspection_tick_at = 0
        self.operations_automation = OperationsAutomation(store)
        self.evolution = EvolutionStore(store, self.agent_manifests)
        self.operations_automation.evolution = self.evolution
        self.targets = TargetStore(store)
        self.remote = RemoteTargets(self.targets)
        self.inspections = InspectionStore(store, self.operations, automation=self.operations_automation, targets=self.targets)
        self.operations_thread = None

    def _ensure_scheduler(self):
        with self.lock:
            if self.stopping.is_set():
                raise Conflict('工厂正在停止，任务保留在队列中')
            self.queue.acquire()
            if self.scheduler is None:
                self.scheduler = threading.Thread(target=self._dispatch, daemon=True,
                                                  name='factory-durable-queue')
                self.operations_thread = threading.Thread(target=self.operations_automation.serve,
                    args=(self.stopping,), daemon=True, name='factory-operations-observer')
                self.operations_thread.start()
                self.scheduler.start()

    def _dispatch(self):
        while not self.stopping.is_set():
            self.wake.wait(0.25)
            self.wake.clear()
            with self.lock:
                if self.stopping.is_set():
                    break
                self.futures = {f for f in self.futures if not f.done()}
                self._drain_feedback()
                if time.monotonic() >= self._inspection_tick_at:
                    self._inspection_tick_at = time.monotonic() + 5
                    try:
                        self.inspections.tick()
                    except Exception:
                        logging.getLogger(__name__).exception('Inspection scheduling failed; normal queue continues')
                for job in self.queue.pending():
                    if len(self.active_jobs) >= 4:
                        break
                    rid, phase = job['run_id'], job['phase']
                    if rid in self.active_jobs:
                        continue
                    run = self.store.get(rid)
                    expected = 'received' if phase in ('plan', 'requirement_analysis') else 'queued'
                    if run['status'] != expected:
                        self.queue.reset(rid)
                        continue
                    if not self.queue.claim(rid, phase):
                        continue
                    self.active_jobs[rid] = phase
                    self.cancels[rid] = _RunCancellation()
                    self.futures.add(self.pool.submit(self._job, rid, phase))

    def _drain_feedback(self):
        """Called by the lease-owning scheduler under the service lock."""
        with self.store.connect() as db:
            conversations = [json.loads(r[0]) for r in db.execute("""
                SELECT c.data FROM agent_conversations c
                LEFT JOIN runs r ON r.id=json_extract(c.data, '$.run_id')
                WHERE json_extract(c.data, '$.mode')='do' AND (
                    EXISTS (SELECT 1 FROM json_each(c.data, '$.messages') m
                        WHERE json_extract(m.value, '$.feedback_status')='pending')
                    OR (json_extract(r.data, '$.status')='received'
                        AND json_extract(r.data, '$.feedback_predecessor_id') IS NOT NULL))
                """)]
        for c in conversations:
            try:
                self._drain_conversation_feedback(c)
            except Exception as exc:
                # One broken conversation must not stop dispatch for the team.
                if c.get('feedback_error') != str(exc):
                    self.agents.feedback_error(c['id'], str(exc))

    def _drain_conversation_feedback(self, c):
        if c.get('run_id') in self.active_jobs:
            return
        if any(m.get('feedback_status') == 'pending' for m in c['messages']):
            if self.governance is not None:
                self.governance.require_project(c.get('actor_id'), c['project_id'])
            if c.get('feedback_error'):
                self.agents.feedback_error(c['id'], None)
            successor = self.agents.adopt_feedback(c['id'])
            if successor:
                # The run and adoption committed together; re-scanning a
                # received successor below repairs a crash before enqueue.
                c['run_id'] = successor['id']
        if c.get('run_id'):
            run = self.store.get(c['run_id'])
            if run.get('feedback_predecessor_id') and run['status'] == 'received':
                self.start_plan(run['id'])


    @staticmethod
    def _authorization_request(run):
        # Context compaction must never drop an earlier authorization signal.
        # Keep submitted intent distinct from model-generated history or errors.
        original = Service._submitted_request(run)
        def intent(text):
            return original if original is not None and text == run.get('request') else text
        return '\n\n'.join(dict.fromkeys(intent(text) for text in [
            run.get('root_request', run.get('request', '')),
            *run.get('authorization_requests', []), run.get('request', '')]))

    @staticmethod
    def _submitted_request(run):
        source = run.get('source') or {}
        current = run.get('request', '')
        # A continuation/retry may replace the request. Never substitute its new
        # intent with the predecessor's original input.
        if source.get('compiled_request_sha256') == hashlib.sha256(current.encode()).hexdigest():
            return source.get('original_request', current)
        return current

    @staticmethod
    def _triage_plan(run, plan):
        # Only the platform-built continuous owner prompt can contain trusted
        # operation boilerplate. Model-generated plans retain every risk signal.
        original = Service._submitted_request(run)
        compiled = run.get('request', '')
        if run.get('execution_mode') != 'continuous' or original is None or not compiled:
            return plan
        return {**plan, 'tasks': [{**task, 'prompt': task['prompt'].replace(compiled, original)}
                                 for task in plan['tasks']]}

    def _project_for_run(self, run):
        """Use the verified predecessor checkout without changing project refs."""
        if run.get('project_id') is None and run.get('source', {}).get('type') == 'skill_ingestion':
            record = self.skill_ingestions.get(run['source']['skill_ingestion_id'])
            agent = self.agents.get(record['target_agent_id'])
            return {'id': None, 'name': agent['name'], 'budget_usd': None, 'checks': {}}
        project = self.store.project(run['project_id'])
        if run.get('spec_confirmation') and run.get('requirement_workspace'):
            if self.governance is not None:
                self.governance.require_project(run.get('source', {}).get('actor_id'), run['project_id'])
            return {**project, 'workspace': run['requirement_workspace'], 'base_branch': run['requirement_branch'], 'spec_tree_enabled': True}
        predecessor = run.get('feedback_predecessor_id')
        if (predecessor or run.get('execution_mode') == 'continuous') and self.governance is not None:
            self.governance.require_project(run.get('source', {}).get('actor_id'), run['project_id'])
        if not predecessor:
            return project
        prior = self.store.get(predecessor)
        artifacts = prior.get('artifacts') or {}
        if (prior['project_id'] != run['project_id'] or
                prior.get('conversation_id') != run.get('conversation_id') or
                prior['status'] not in ('ready_for_review', 'published') or
                not all(artifacts.get(k) for k in ('worktree', 'branch', 'commit'))):
            raise ValueError('上一轮成果缺少可验证的接续工作区，请检查后重试')
        def common(root):
            output = subprocess.run(['git', 'rev-parse', '--git-common-dir'], cwd=root,
                check=True, capture_output=True, text=True, timeout=15).stdout.strip()
            return (Path(root) / output).resolve()
        if common(project['workspace']) != common(artifacts['worktree']):
            raise ValueError('接续工作区不属于当前项目')
        result = {**project, 'workspace': artifacts['worktree'], 'base_branch': artifacts['branch']}
        if baseline_sha(result) != artifacts['commit']:
            raise ValueError('上一轮成果分支已变化，不能自动接续')
        return result

    def _job(self, rid, phase):
        try:
            if self.store.get(rid).get('source', {}).get('type') == 'inspection':
                inspect_run(self, rid)
            else:
                (self._analyze if phase == 'requirement_analysis' else self._plan if phase == 'plan' else self._run)(rid)
        finally:
            with self.lock:
                self.queue.finish(rid, phase)
                self.active_jobs.pop(rid, None)
                self.wake.set()
            # Auto-consume pending followups at this safe node.  Called AFTER
            # active_jobs.pop so the rid-in-active_jobs guard does not block it,
            # and outside the lock block so _auto_resume_with_followups can
            # acquire svc.lock cleanly (even though RLock would allow re-entry,
            # keeping the critical section minimal is safer).
            from factory.control.run_lifecycle import _auto_resume_with_followups, _collect_pending_followups
            if self.store.get(rid)['status'] == 'needs_human' and _collect_pending_followups(self, rid):
                _auto_resume_with_followups(self, rid)

    def _submit(self, fn, rid):
        with self.lock:
            self._ensure_scheduler()
            phase = 'requirement_analysis' if fn == self._analyze else 'plan' if fn == self._plan else 'execute'
            # A user can answer a just-created clarification before the old
            # planning worker's finally block has released its slot. Preserve
            # that continuation; the same-run active slot prevents overlap.
            continuation = phase == 'plan' and self.store.get(rid)['status'] == 'received'
            self.queue.enqueue(rid, phase, continuation=continuation)
            self.wake.set()

    def _record_interrupted_provider_usage(self, run):
        'Close each in-flight provider lane with a durable unknown-cost hold.'
        return run_billing._record_interrupted_provider_usage(self, run)

    def recover(self):
        'Recover unstarted work automatically; preserve ambiguous writes for reconciliation.'
        return recovery.recover(self)

    def _analyze(self, rid):
        return requirement_analysis.analyze(self, rid)

    def start_plan(self, rid):
        if requirement_analysis.required(self.store.get(rid)):
            self._submit(self._analyze, rid)
            return
        if self.store.get(rid).get('source', {}).get('skill_ingestion_id'):
            self._submit(self._plan, rid)
            return
        if self.store.get(rid).get('source', {}).get('type') == 'inspection':
            raise Conflict('巡检只记录诊断；需要修复时请另行提交维护任务')
        with self.lock:
            run = self.store.get(rid)
            if 'execution_mode' not in run and not run.get('plan'):
                policy = run.get('policy') or self.policies.get(run['project_id'])
                eligible = run.get('source', {}).get('type') in ('web', 'agent', 'capability', 'retry')
                mode = 'continuous' if eligible and policy['mode'] == 'autonomous' else 'dag'
                run = self.store.update(rid, {'execution_mode': mode}, expected=('received',),
                    event=('execution.mode_selected', {'mode': mode}))
            module_state = ModuleStore(self.store).freeze(run)
            if module_state:
                run = self.store.update(rid, module_state, expected=('received',),
                    event=('modules.frozen', {'modules': [{'id': m['id'], 'version': m['version'], 'name': m['name']} for m in module_state['module_snapshot']]}))
            frozen = ProjectAssistants(self.store).freeze(run, self.runtime_settings.get())
            if frozen:
                self.store.update(rid, frozen, expected=('received',), event=('agent.version_frozen', {
                    'agent_id': frozen['agent_id'], 'version': frozen['agent_version'],
                    'project_assistant_revision': frozen['project_assistant_revision']}))
        self._submit(self._plan, rid)

    def start_maintenance(self, callback, *, job_id=None, conversation_id='', actor_id=0):
        """Bounded background turns, with durable claims and explicit recovery."""
        job_id = job_id or uuid.uuid4().hex
        cancel = threading.Event()
        with self.lock:
            if self.stopping.is_set():
                raise Conflict('工厂正在停止，请稍后重试')
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                active = db.execute("SELECT conversation_id FROM maintenance_jobs WHERE status IN ('pending','running','cancel_requested')").fetchall()
                if any(row['conversation_id'] == conversation_id for row in active):
                    raise Conflict('这段对话已有处理中请求，请等待完成或取消')
                if len(active) >= 4:
                    raise Conflict('当前对话处理已满，请稍后重试')
                at = now()
                db.execute('INSERT INTO maintenance_jobs(id,conversation_id,actor_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                           (job_id, conversation_id, actor_id, 'pending', at, at))
            self.maintenance_cancels[job_id] = cancel

            def worker():
                status, result, error = 'completed', None, None
                try:
                    if cancel.is_set():
                        raise ProviderCancelled('调用已取消')
                    with self.store.connect() as db:
                        db.execute("UPDATE maintenance_jobs SET status='running',updated_at=? WHERE id=? AND status='pending'", (now(), job_id))
                    result = callback(cancel)
                    if isinstance(result, dict) and result.get('status') == 'cancelled':
                        status = 'cancelled'
                except Exception as exc:
                    status = 'cancelled' if cancel.is_set() or isinstance(exc, ProviderCancelled) else 'failed'
                    error = scrub(str(exc))[:2000]
                    handler = getattr(callback, 'on_error', None)
                    if callable(handler):
                        try:
                            handler(ProviderCancelled('调用已取消') if status == 'cancelled' else exc)
                        except Exception:
                            pass
                finally:
                    with self.store.connect() as db:
                        db.execute('UPDATE maintenance_jobs SET status=?,result=?,error=?,updated_at=? WHERE id=?',
                                   (status, json.dumps(scrub(result), ensure_ascii=False), error, now(), job_id))
                    with self.lock:
                        self.maintenance_cancels.pop(job_id, None)
                    self.wake.set()

            try:
                self.pool.submit(worker)
            except RuntimeError:
                with self.store.connect() as db:
                    db.execute("UPDATE maintenance_jobs SET status='interrupted',error='任务未能派发',updated_at=? WHERE id=?", (now(), job_id))
                self.maintenance_cancels.pop(job_id, None)
                raise Conflict('任务未能派发，请稍后重试') from None
        return self.maintenance_status(job_id)

    def maintenance_status(self, job_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM maintenance_jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        job = dict(row)
        job['result'] = json.loads(job['result']) if job.get('result') else None
        return job

    def cancel_maintenance(self, job_id, actor):
        with self.lock:
            job = self.maintenance_status(job_id)
            if job['status'] in ('completed', 'failed', 'cancelled', 'interrupted'):
                return job
            cancel = self.maintenance_cancels.get(job_id)
            if cancel:
                cancel.set()
            with self.store.connect() as db:
                db.execute("UPDATE maintenance_jobs SET status='cancel_requested',updated_at=? WHERE id=? AND status IN ('pending','running')", (now(), job_id))
            return self.maintenance_status(job_id)

    def _waiting_policy_check(self, run, project, policy):
        """Re-triage a waiting plan and return ``(reason, decision)``."""
        if policy.get('mode') != 'autonomous':
            return '仅自主模式可以自动接续待处理计划', {
                'decision': 'human_approval', 'questions': [],
                'reasons': ['仅自主模式可以自动接续待处理计划'], 'risk': 'high',
            }
        plan = run.get('plan')
        if not isinstance(plan, dict):
            return '运行尚未形成可执行计划', {'decision': 'human_approval', 'questions': [],
                                             'reasons': ['运行尚未形成可执行计划'], 'risk': 'high'}
        source = run.get('source') if isinstance(run.get('source'), dict) else {}
        issue_auto = (project.get('auto_issues', False) and source.get('type') == 'github'
                      and source.get('trusted_label', False) and not source.get('previous_run_id'))
        eligible = source.get('type') in ('web', 'capability', 'retry', 'agent') or issue_auto
        decision = fresh_triage(self._triage_plan(run, plan), self._authorization_request(run), auto_enabled=eligible)
        prior_triage = run.get('triage') if isinstance(run.get('triage'), dict) else {}
        questions = list(dict.fromkeys([
            *(question for question in prior_triage.get('questions', []) if isinstance(question, str)),
            *(question for question in plan.get('questions', []) if isinstance(question, str)),
            *(question for question in decision.get('questions', []) if isinstance(question, str)),
        ]))
        decision['questions'] = questions
        if source.get('previous_run_id'):
            decision['decision'] = 'human_approval'
            decision['reasons'].append('该 Issue 已有运行记录；当前计划需要人工核对前次变更')
        decision = policy_decision(decision, policy, eligible=eligible)
        if decision.get('questions'):
            decision['decision'] = 'needs_clarification'
            return '需求尚有待澄清问题', decision
        if decision.get('decision') != 'auto_execute':
            return (decision.get('reasons') or ['当前来源或风险范围不满足自主执行条件'])[-1], decision
        configuration = run.get('runtime_configuration') or self.runtime_settings.get()
        if len(plan.get('tasks') or []) > configuration['limits']['max_tasks']:
            reason = '计划任务数超过运行限制，请重新规划'
            decision['decision'], decision['reasons'] = 'human_approval', [reason]
            return reason, decision
        try:
            for task in {profile_for(task) for task in plan.get('tasks') or []}:
                self._check_profile(configuration['profiles'][task], task)
        except (Conflict, KeyError) as exc:
            reason = str(exc)
            decision['decision'], decision['reasons'] = 'human_approval', [reason]
            return reason, decision
        usage = self._usage(run['id'])
        try:
            if run.get('feedback_predecessor_id'):
                project = self._project_for_run(run)
            if run.get('context') and baseline_sha(project) != run['context']['commit_sha']:
                reason = '工程基线已变化，请重新规划后继续'
                decision['decision'], decision['reasons'] = 'human_approval', [reason]
                return reason, decision
        except Exception as exc:
            reason = f'无法核对工程基线：{exc}'
            decision['decision'], decision['reasons'] = 'human_approval', [reason]
            return reason, decision
        for task in plan.get('tasks') or []:
            if not task.get('acceptance') or not task.get('paths') or not task.get('checks'):
                reason = '任务缺少验收标准、修改范围或已配置检查'
                decision['decision'], decision['reasons'] = 'human_approval', [reason]
                return reason, decision
            if any(check not in project.get('checks', {}) for check in task['checks']):
                reason = '项目检查配置已变化，请重新规划'
                decision['decision'], decision['reasons'] = 'human_approval', [reason]
                return reason, decision
        return None, decision

    def _apply_waiting_policy_locked(self, project_id, policy, actor):
        project = self.store.project(project_id)
        continued, blocked = [], []
        for snapshot in self.store.all_runs():
            if snapshot.get('project_id') != project_id or snapshot.get('status') != 'awaiting_approval':
                continue
            rid = snapshot['id']
            reason, decision = self._waiting_policy_check(snapshot, project, policy)
            payload = {'policy_revision': policy['revision'], 'plan_revision': snapshot.get('revision'),
                       'previous_policy_revision': (snapshot.get('policy') or {}).get('revision'),
                       'previous_policy': snapshot.get('policy'),
                       'actor': actor, 'decision': decision.get('decision')}
            if reason:
                payload['reason'] = reason
            try:
                adopted = self.store.update(rid, {'policy': policy, 'triage': decision},
                                            expected=('awaiting_approval',), revision=snapshot['revision'],
                                            event=('policy.adopted', payload))
                if reason:
                    blocked.append({'run_id': rid, 'reason': reason})
                    continue
                self.approve(rid, adopted['revision'], actor='project-policy')
                continued.append(rid)
            except Conflict as exc:
                blocked.append({'run_id': rid, 'reason': str(exc)})
        return {'continued_run_ids': continued, 'blocked': blocked}

    def update_policy(self, project_id, values, revision, actor, *, apply_waiting=False):
        """Persist a policy and optionally adopt it under one service lock."""
        with self.lock:
            policy = self.policies.update(project_id, values, revision, actor)
            application = None
            if apply_waiting and policy['mode'] == 'autonomous':
                application = self._apply_waiting_policy_locked(project_id, policy, actor)
            return policy, application

    def apply_waiting_policy(self, project_id, policy, actor):
        """Adopt an explicitly autonomous policy for waiting plans once, safely."""
        with self.lock:
            return self._apply_waiting_policy_locked(project_id, policy, actor)

    def _check_profile(self, profile, role):
        if not profile.get('model'):
            raise Conflict(f'请先在运行配置中设置 {role} 的模型，再重新规划')
        if self.check_runtime:
            blockers = profile_blockers(profile, role)
            if blockers:
                raise Conflict('；'.join(blockers))

    def _runner_for(self, rid):
        return MountedRunner(self.runner, self.store, rid, self.governance)

    def _legacy_claude_budget_stop(self, rid, artifacts, project):
        'Recognize the one pre-classification Claude ceiling failure safely.'
        return run_billing._legacy_claude_budget_stop(self, rid, artifacts, project)

    def _reconciled_usage_calls(self, rid, *, profile=None):
        'Collapse durable accounting rows into logical provider calls.'
        return run_billing._reconciled_usage_calls(self, rid, profile=profile)

    def _budget_usage(self, rid, project):
        'Build a pessimistic ledger for calls whose invoice is unresolved.\n\n        A later row with the same call id and a known cost reconciles the hold.\n        Historical rows without a recorded ceiling reserve the whole finite run\n        budget, because allowing another paid call would make the hard cap\n        unenforceable after a restart.\n        '
        return run_billing._budget_usage(self, rid, project)

    def _dollar_budget(self, rid, project):
        'Read durable usage immediately before a paid project call.'
        return run_billing._dollar_budget(self, rid, project)

    def _remaining_dollar_budget(self, rid, project):
        return run_billing._remaining_dollar_budget(self, rid, project)

    def _budget_stop_artifacts(self, rid, project, reason, artifacts=None):
        'Persist a structured, user-visible reason for every service budget stop.'
        return run_billing._budget_stop_artifacts(self, rid, project, reason, artifacts)

    def _emit(self, rid, kind, payload, task_id=None):
        return run_execution._emit(self, rid, kind, payload, task_id)

    def _fail(self, rid, exc):
        return run_lifecycle._fail(self, rid, exc)

    def _plan(self, rid):
        return run_execution._plan(self, rid)

    def clarify(self, rid, answer, actor, *, feedback_message_ids=None):
        return run_lifecycle.clarify(self, rid, answer, actor, feedback_message_ids=feedback_message_ids)

    def continue_run(self, rid, answer, revision, resume_count, actor):
        return run_lifecycle.continue_run(self, rid, answer, revision, resume_count, actor)

    def approve(self, rid, revision, actor):
        return run_lifecycle.approve(self, rid, revision, actor)

    def _run(self, rid):
        return run_execution._run(self, rid)

    def _independent_verify(self, rid, run, project, configuration, artifacts):
        return verification._independent_verify(self, rid, run, project, configuration, artifacts)

    def _verify_snapshot(self, rid, run, project, configuration, artifacts, workspace, coverage_retry=False):
        'Ask the configured verification model for a bounded evidence verdict.'
        return verification._verify_snapshot(self, rid, run, project, configuration, artifacts, workspace, coverage_retry)

    def github_options(self, rid, *, page=1):
        return run_lifecycle.github_options(self, rid, page=page)

    def bind_github_repository(self, rid, repository, expected_project_revision, actor):
        return run_lifecycle.bind_github_repository(self, rid, repository, expected_project_revision, actor)

    def publish_github(self, rid, **kwargs):
        return run_lifecycle.publish_github(self, rid, **kwargs)

    def publish(self, rid):
        return run_lifecycle.publish(self, rid)

    def cancel(self, rid, actor):
        return run_lifecycle.cancel(self, rid, actor)

    def discard(self, rid, actor):
        'Retire obsolete work from operational attention while retaining all evidence.'
        return run_lifecycle.discard(self, rid, actor)

    def _usage(self, rid, *, profile=None):
        return run_billing._usage(self, rid, profile=profile)

    def _capture_capability(self, rid):
        'Harvest a traceable draft; candidate extraction cannot fail a delivery.'
        return run_lifecycle._capture_capability(self, rid)

    def retry(self, rid, actor, actor_id=None):
        return run_lifecycle.retry(self, rid, actor, actor_id)

    def sync_merge(self, rid):
        'Observe GitHub; never merge a PR or treat a plan claim as established fact.'
        return run_lifecycle.sync_merge(self, rid)

    def close(self):
        self.stopping.set()
        self.wake.set()
        if self.scheduler is not None:
            self.scheduler.join(timeout=2)
        for event in self.cancels.values():
            event.set()
        for event in self.maintenance_cancels.values():
            event.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        if self.operations_thread is not None:
            self.operations_thread.join(timeout=6)
        self.queue.release()
        if self.publisher and hasattr(self.publisher, 'close'):
            self.publisher.close()
