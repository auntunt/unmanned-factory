"""Application coordinator; all durable transitions are guarded and auditable."""
from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from factory.control.store import ACTIVE, Conflict, Store, now
from factory.control.autonomy import (DurableQueue, PolicyStore, all_events,
    capability_context, capability_prompt, policy_decision, valid_cost)


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
                 execute=None, timeout_s=600, max_parallel=2):
        from factory.control.providers import SDKRunner
        from factory.control.execution import execute_plan
        from factory.control.runtime import RuntimeSettings
        self.store = store
        self.runner = runner or SDKRunner()
        self.governance = None
        self.publisher = publisher
        self.runtime_settings = RuntimeSettings(store, profiles=profiles or configured_profiles(),
            limits={'timeout_s': timeout_s, 'max_parallel': max_parallel})
        self.profiles = self.runtime_settings.get()['profiles']
        self.check_runtime = runner is None or isinstance(runner, SDKRunner)
        self.execute = execute or execute_plan
        self.timeout_s = timeout_s
        self.max_parallel = max_parallel
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='factory-control')
        self.cancels = {}
        self.lock = threading.RLock()
        self.futures = set()
        self.queue = DurableQueue(store)
        self.policies = PolicyStore(store)
        self.active_jobs = {}
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.scheduler = None

    def _ensure_scheduler(self):
        with self.lock:
            if self.stopping.is_set():
                raise Conflict('工厂正在停止，任务保留在队列中')
            self.queue.acquire()
            if self.scheduler is None:
                self.scheduler = threading.Thread(target=self._dispatch, daemon=True,
                                                  name='factory-durable-queue')
                self.scheduler.start()

    def _dispatch(self):
        while not self.stopping.is_set():
            self.wake.wait(0.25)
            self.wake.clear()
            with self.lock:
                if self.stopping.is_set():
                    break
                self.futures = {f for f in self.futures if not f.done()}
                for job in self.queue.pending():
                    if len(self.active_jobs) >= 4:
                        break
                    rid, phase = job['run_id'], job['phase']
                    if rid in self.active_jobs:
                        continue
                    run = self.store.get(rid)
                    expected = 'received' if phase == 'plan' else 'queued'
                    if run['status'] != expected:
                        self.queue.reset(rid)
                        continue
                    if not self.queue.claim(rid, phase):
                        continue
                    self.active_jobs[rid] = phase
                    self.cancels[rid] = threading.Event()
                    self.futures.add(self.pool.submit(self._job, rid, phase))

    def _job(self, rid, phase):
        try:
            (self._plan if phase == 'plan' else self._run)(rid)
        finally:
            with self.lock:
                self.queue.finish(rid, phase)
                self.active_jobs.pop(rid, None)
                self.wake.set()

    def _submit(self, fn, rid):
        with self.lock:
            self._ensure_scheduler()
            phase = 'plan' if fn == self._plan else 'execute'
            # A user can answer a just-created clarification before the old
            # planning worker's finally block has released its slot. Preserve
            # that continuation; the same-run active slot prevents overlap.
            continuation = phase == 'plan' and self.store.get(rid)['status'] == 'received'
            self.queue.enqueue(rid, phase, continuation=continuation)
            self.wake.set()

    def recover(self):
        """Recover unstarted work automatically; preserve ambiguous writes for reconciliation."""
        with self.lock:
            self.queue.acquire()
            if self.governance is not None:
                self.governance.recover()
            for run in self.store.all_runs():
                if run['status'] in ('ready_for_review', 'published') and run.get('policy') and not run.get('capability_candidate_id'):
                    self._capture_capability(run['id'])
                if run['status'] not in ACTIVE:
                    continue
                rid = run['id']
                try:
                    policy = run.get('policy') or self.policies.get(run['project_id'])
                except KeyError:
                    policy = {'resume_on_restart': False}
                self.queue.reset(rid)
                if policy['resume_on_restart'] and run['status'] in ('received', 'planning', 'queued'):
                    phase = 'execute' if run['status'] == 'queued' else 'plan'
                    if run['status'] == 'planning':
                        events = list(all_events(self.store, rid))
                        starts = [e for e in events if e['type'] == 'provider.started'
                                  and e['payload'].get('profile') == 'planner']
                        last_start = starts[-1] if starts else None
                        if last_start and not any(e['type'] == 'usage.recorded'
                                and e['payload'].get('profile') == 'planner'
                                and e['id'] > last_start['id'] for e in events):
                            self.store.append(rid, 'usage.recorded', {**last_start['payload'],
                                'cost_usd': None, 'interrupted': True,
                                'message': '规划调用中断，未确认的费用不能记为零'})
                    self.store.update(rid, {'status': 'queued' if phase == 'execute' else 'received'},
                        expected=(run['status'],), event=('run.resumed',
                        {'phase': phase, 'message': '恢复持久队列，继续未完成的工作'}))
                    self.queue.enqueue(rid, phase)
                else:
                    checkpoints = [e['payload'] for e in all_events(self.store, rid)
                                   if e['type'] == 'execution.checkpoint']
                    self.store.update(rid, {'status': 'needs_human',
                        'recovery': {'previous_status': run['status'],
                                     'checkpoint': checkpoints[-1] if checkpoints else None}},
                        expected=(run['status'],), event=('run.recovered',
                        {'message': '执行已中断，保留工作区与检查点；核对已发生的写入后可从记录重试，避免重复发布。'}))
            self._ensure_scheduler()
            self.wake.set()

    def start_plan(self, rid):
        self._submit(self._plan, rid)

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
        eligible = source.get('type') in ('web', 'capability', 'retry') or issue_auto
        from factory.control.planning import triage as fresh_triage
        decision = fresh_triage(plan, run.get('request', ''), auto_enabled=eligible)
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
        from factory.control.planning import profile_for
        try:
            for task in {profile_for(task) for task in plan.get('tasks') or []}:
                self._check_profile(configuration['profiles'][task], task)
        except (Conflict, KeyError) as exc:
            reason = str(exc)
            decision['decision'], decision['reasons'] = 'human_approval', [reason]
            return reason, decision
        usage = self._usage(run['id'])
        if usage['known_cost_usd'] >= project['budget_usd']:
            reason = '本次运行预算已用尽，停止新的执行调用'
            decision['decision'], decision['reasons'] = 'human_approval', [reason]
            return reason, decision
        if usage['unknown_cost_calls'] and configuration['limits']['unknown_cost_policy'] == 'stop':
            reason = '已有模型调用费用未知，当前策略停止继续'
            decision['decision'], decision['reasons'] = 'human_approval', [reason]
            return reason, decision
        from factory.control.codegraph import baseline_sha
        try:
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
            from factory.control.runtime import profile_blockers
            blockers = profile_blockers(profile, role)
            if blockers:
                raise Conflict('；'.join(blockers))

    def _runner_for(self, rid):
        if self.governance is None:
            return self.runner
        from factory.control.governance import GovernedRunner
        return GovernedRunner(self.runner, self.governance, rid)

    def _emit(self, rid, kind, payload, task_id=None):
        self.store.append(rid, kind, payload, task_id)
        if kind == 'execution.checkpoint':
            self.store.update(rid, {'checkpoint': payload})
        if task_id and kind in ('task.started', 'task.completed', 'task.failed'):
            with self.lock:
                run = self.store.get(rid)
                tasks = run['tasks']
                for task in tasks:
                    if task['id'] == task_id:
                        task['status'] = {'task.started': 'running', 'task.completed': 'completed',
                                          'task.failed': 'failed'}[kind]
                self.store.update(rid, {'tasks': tasks})

    def _fail(self, rid, exc):
        from factory.control.store import scrub
        with self.lock:
            run = self.store.get(rid)
            if run['status'] == 'cancelled':
                return
            changes = {'status': 'needs_human'}
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
                event=('run.failed', {'message': scrub(str(exc))[:2000], 'error_type': type(exc).__name__}))

    def _plan(self, rid):
        from factory.control.planning import build_prompt, parse_plan, triage
        from factory.control.providers import ProviderRequest
        try:
            run = self.store.update(rid, {'status': 'planning'}, expected=('received',),
                                    event=('run.planning', {'message': '正在梳理需求与验收条件'}))
            project = self.store.project(run['project_id'])
            policy = run.get('policy') or self.policies.get(project['id'])
            snapshots = run.get('capabilities')
            if snapshots is None:
                snapshots = capability_context(self.store, run)
            self.store.update(rid, {'policy': policy, 'capabilities': snapshots}, expected=('planning',),
                              event=('policy.frozen', {'policy': policy,
                                     'capabilities': [{'id': c['id'], 'revision': c['revision']} for c in snapshots]}))
            configuration = self.runtime_settings.get()
            profile = configuration['profiles']['planner']
            self.store.update(rid, {'runtime_configuration': configuration}, expected=('planning',),
                event=('runtime.configuration_frozen', configuration))
            try:
                self._check_profile(profile, 'planner')
            except Conflict as exc:
                # A configuration blocker is a recoverable failure, not a
                # cancellation race. Keep it visible with a next action.
                raise ValueError(str(exc)) from None
            from factory.control.context import assemble_context, verify_planning_checkout
            context = assemble_context(self.store, project, run['request'], run['history'])
            verify_planning_checkout(project, context['commit_sha'])
            self.store.update(rid, {'context': context}, expected=('planning',),
                              event=('context.assembled', context))
            ledger = self._usage(rid)
            if ledger['known_cost_usd'] >= project['budget_usd']:
                raise ValueError('本次需求已达到预算，停止新的规划调用')
            if ledger['unknown_cost_calls'] and configuration['limits']['unknown_cost_policy'] == 'stop':
                raise ValueError('已有调用费用未知，当前策略停止继续；可在运行配置中选择有界继续后重新规划')
            # A governed call has not reached a provider until its token
            # reservation succeeds.  Keep the durable "started" marker (and
            # its corresponding usage record) behind that boundary so a
            # quota denial cannot be mistaken for an unpriced model call.
            call_id = uuid.uuid4().hex
            dispatched = self.governance is None
            if dispatched:
                self._emit(rid, 'provider.started', {'profile': 'planner', **profile, 'call_id': call_id}, 'planner')
            result = None

            def planning_emit(kind, payload):
                nonlocal call_id, dispatched
                self._emit(rid, kind, payload, 'planner')
                if kind == 'quota.reserved':
                    reserved_id = payload.get('id') if isinstance(payload, dict) else None
                    if isinstance(reserved_id, str) and reserved_id:
                        call_id = reserved_id
                    dispatched = True
                    self._emit(rid, 'provider.started', {'profile': 'planner', **profile, 'call_id': call_id}, 'planner')

            try:
                result = self._runner_for(rid).run(ProviderRequest(provider=profile['provider'], model=profile['model'],
                    prompt=build_prompt(run['request'], project, run['history'], context=context) + capability_prompt(snapshots), workspace=project['workspace'],
                    timeout_s=configuration['limits']['timeout_s'], read_only=True),
                    planning_emit, self.cancels[rid])
            finally:
                if dispatched:
                    usage = {'profile': 'planner', **profile, 'call_id': call_id, 'cost_usd': valid_cost(getattr(result, 'cost_usd', None)),
                             'input_tokens': getattr(result, 'tokens_in', None),
                             'output_tokens': getattr(result, 'tokens_out', None),
                             'cached_input_tokens': getattr(result, 'cached_input_tokens', None)}
                    self._emit(rid, 'usage.recorded', usage, 'planner')
                self.store.update(rid, {'planner_usage': self._usage(rid, profile='planner')})
            verify_planning_checkout(project, context['commit_sha'])
            plan = parse_plan(result.text, project)
            if len(plan['tasks']) > configuration['limits']['max_tasks']:
                raise ValueError('计划任务数超过运行配置限制；请缩小需求或调整限制后重新规划')
            source = run['source']
            auto = (project.get('auto_issues', False) and source.get('type') == 'github'
                    and source.get('trusted_label', False) and not source.get('previous_run_id'))
            decision = triage(plan, run['request'], auto_enabled=auto)
            # Web input and explicitly invoked capability contracts are owner
            # requests. An untrusted issue body cannot grant itself autonomy.
            decision = policy_decision(decision, policy,
                eligible=source.get('type') in ('web', 'capability', 'retry') or auto)
            if source.get('previous_run_id'):
                decision['reasons'].append('该 Issue 已有运行记录；内容更新后需要人工核对前次变更和当前计划')
            status = 'needs_clarification' if decision['decision'] == 'needs_clarification' else 'awaiting_approval'
            updated = self.store.update(rid, {'status': status, 'revision': run['revision'] + 1,
                'plan': plan, 'triage': decision,
                'tasks': [{**t, 'status': 'pending'} for t in plan['tasks']]}, expected=('planning',),
                event=('plan.created', {'revision': run['revision'] + 1, 'summary': plan['summary'], 'plan': plan}))
            self._emit(rid, 'triage.decided', decision)
            if decision['decision'] == 'auto_execute':
                self.approve(rid, updated['revision'], actor='project-policy')
        except Conflict as exc:
            # Only a competing state transition is benign. Configuration and
            # policy blockers must not leave an apparently healthy waiting run.
            if self.store.get(rid)['status'] in ('planning', 'awaiting_approval'):
                self._fail(rid, exc)
        except Exception as exc:
            self._fail(rid, exc)

    def clarify(self, rid, answer, actor):
        with self.lock:
            run = self.store.get(rid)
            updated = self.store.update(rid, {'status': 'received', 'plan': None, 'triage': None,
                'history': [*run['history'], answer], 'tasks': [], 'context': None,
                'runtime_configuration': None},
                expected=('needs_clarification', 'awaiting_approval', 'needs_human'),
                event=('user.message', {'text': answer, 'actor': actor, 'revision': run['revision']}))
            try:
                self.start_plan(rid)
            except Exception as exc:
                self._fail(rid, exc)
                raise
            return updated

    def approve(self, rid, revision, actor):
        with self.lock:
            run = self.store.get(rid)
            if not run.get('plan') or run['plan'].get('questions'):
                raise Conflict('需求尚有待澄清问题')
            project = self.store.project(run['project_id'])
            configuration = run.get('runtime_configuration') or self.runtime_settings.get()
            if len(run['plan']['tasks']) > configuration['limits']['max_tasks']:
                raise Conflict('计划任务数超过运行限制，请重新规划')
            from factory.control.planning import profile_for
            for role in {profile_for(task) for task in run['plan']['tasks']}:
                self._check_profile(configuration['profiles'][role], role)
            usage = self._usage(rid)
            if usage['known_cost_usd'] >= project['budget_usd']:
                raise Conflict('本次运行预算已用尽，停止新的执行调用')
            if usage['unknown_cost_calls'] and configuration['limits']['unknown_cost_policy'] == 'stop':
                raise Conflict('已有模型调用费用未知；请配置有界继续策略后重新规划')
            from factory.control.codegraph import baseline_sha
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

    def _run(self, rid):
        try:
            run = self.store.update(rid, {'status': 'running'}, expected=('queued',),
                                    event=('run.started', {}))
            project = self.store.project(run['project_id'])
            configuration = run.get('runtime_configuration') or self.runtime_settings.get()
            limits = configuration['limits']
            project = {**project, 'max_tasks': limits['max_tasks'],
                       'unknown_cost_policy': limits['unknown_cost_policy']}
            prior_usage = self._usage(rid)
            project['budget_usd'] = project['budget_usd'] - prior_usage['known_cost_usd']
            policy = run.get('policy') or self.policies.get(project['id'])
            if policy.get('revision', 0) or policy['mode'] == 'autonomous':
                project['routing_policy'] = {key: policy[key] for key in ('max_attempts', 'auto_escalate')}
            if run.get('context'):
                project = {**project, 'expected_base_sha': run['context']['commit_sha']}
            from factory.control.context import context_prompt
            plan = {**run['plan'], 'tasks': [
                {**task, 'prompt': task['prompt'] + context_prompt(run.get('context')) + capability_prompt(run.get('capabilities', []))}
                for task in run['plan']['tasks']]}
            # A clarified goal can have an earlier failed execution workspace.
            # Retain that evidence and allocate the new plan its own refs.
            execution_id = rid if run['revision'] == 1 else f"{rid}-r{run['revision']}"
            artifacts = self.execute(run_id=execution_id, plan=plan, project=project,
                profiles=configuration['profiles'], runner=self._runner_for(rid),
                emit=lambda kind, payload, task_id=None: self._emit(rid, kind, payload, task_id),
                cancel=self.cancels[rid], max_parallel=limits['max_parallel'], timeout_s=limits['timeout_s'])
            if prior_usage['unknown_cost_calls']:
                artifacts['billing_incomplete'] = '规划阶段存在未报告费用'
                artifacts['autopublish_blocked'] = True
            execution_known = valid_cost(artifacts.get('known_cost_usd')) or 0.0
            artifacts['total_known_cost_usd'] = execution_known + prior_usage['known_cost_usd']
            artifacts['planner_cost_usd'] = self._usage(rid, profile='planner')['known_cost_usd']
            tasks = artifacts.get('tasks') or [{**t, 'status': 'completed'} for t in run['tasks']]
            self.store.update(rid, {'status': 'ready_for_review', 'artifacts': artifacts, 'tasks': tasks},
                expected=('running', 'verifying'), event=('run.verified', artifacts))
            self._capture_capability(rid)
            if project.get('auto_publish') and not artifacts.get('autopublish_blocked') and not artifacts.get('billing_incomplete'):
                self.publish(rid)
        except Conflict as exc:
            status = self.store.get(rid)['status']
            if status in ('running', 'verifying'):
                self._fail(rid, exc)
            elif status == 'ready_for_review':
                self._emit(rid, 'delivery.blocked', {'message': str(exc)})
        except Exception as exc:
            self._fail(rid, exc)

    def publish(self, rid):
        if not self.publisher:
            raise Conflict('尚未配置 GitHub 发布凭据')
        run = self.store.update(rid, {'status': 'publishing'}, expected=('ready_for_review',),
                                event=('github.publish_started', {}))
        try:
            delivery = self.publisher.publish(self.store.project(run['project_id']), run)
            return self.store.update(rid, {'status': 'published', 'artifacts': {**run['artifacts'], **delivery}},
                expected=('publishing',), event=('github.published', delivery))
        except Exception as exc:
            self.store.update(rid, {'status': 'ready_for_review'}, expected=('publishing',),
                              event=('github.publish_failed', {'message': str(exc)}))
            raise

    def cancel(self, rid, actor):
        with self.lock:
            self.cancels.setdefault(rid, threading.Event()).set()
            return self.store.update(rid, {'status': 'cancelled'},
                expected=('received', 'planning', 'queued', 'running', 'verifying', 'awaiting_approval',
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
            }, expected=('needs_clarification', 'awaiting_approval', 'needs_human',
                         'failed', 'ready_for_review', 'cancelled'),
               event=('run.discarded', {
                   'message': '旧任务已标记废弃；保留计划、费用、日志和工作区记录',
                   'actor': actor, 'previous_status': run['status'],
               }))

    def _usage(self, rid, *, profile=None):
        costs = [valid_cost(e['payload'].get('cost_usd')) for e in all_events(self.store, rid)
                 if e['type'] == 'usage.recorded' and (profile is None or e['payload'].get('profile') == profile)]
        total = sum(cost for cost in costs if cost is not None)
        if not __import__('math').isfinite(total):
            raise Conflict('累计费用超出可表示范围，停止派发')
        return {'known_cost_usd': total, 'unknown_cost_calls': sum(cost is None for cost in costs), 'calls': len(costs)}

    def _capture_capability(self, rid):
        """Harvest a traceable draft; candidate extraction cannot fail a delivery."""
        from factory.control.capabilities import CapabilityStore
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

    def retry(self, rid, actor, actor_id=None):
        with self.lock:
            prior = self.store.get(rid)
            if prior['status'] not in ('needs_human', 'failed', 'cancelled'):
                raise Conflict('仅中断、失败或取消的运行可重新规划')
            failure = next((e['payload'].get('message', '') for e in reversed(list(all_events(self.store, rid)))
                            if e['type'] in ('run.failed', 'run.recovered')), '')
            run, created = self.store.create_run(prior['project_id'], prior['request'],
                source={'type': 'retry', 'actor': actor, 'actor_id': actor_id, 'retry_of': rid}, delivery_id=f'retry:{rid}:{prior["revision"]}')
            if created:
                history = [*prior.get('history', []), f'前次运行失败，保留证据以便修复：{failure[:2000]}']
                self.store.update(run['id'], {'history': history, 'previous_run_id': rid,
                    **({'capability': prior['capability']} if prior.get('capability') else {})},
                    event=('run.retry_created', {'previous_run_id': rid, 'actor': actor}))
                self.store.append(rid, 'run.retry_linked', {'next_run_id': run['id'], 'actor': actor})
                self.start_plan(run['id'])
            return self.store.get(run['id'])

    def sync_merge(self, rid):
        """Observe GitHub; never merge a PR or treat a plan claim as established fact."""
        from factory.control.knowledge import KnowledgeStore
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

    def close(self):
        self.stopping.set()
        self.wake.set()
        if self.scheduler is not None:
            self.scheduler.join(timeout=2)
        for event in self.cancels.values():
            event.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.queue.release()
        if self.publisher and hasattr(self.publisher, 'close'):
            self.publisher.close()
