"""Planning, coding and repair orchestration with the existing event projection order."""
from __future__ import annotations

import time
import uuid

from factory.control.acceptance_ledger import repair_guidance
from factory.control.autonomy import capability_context, capability_prompt, policy_decision, valid_cost
from factory.control.context import assemble_context, context_prompt, verify_planning_checkout
from factory.control.continuous import execute_continuous
from factory.control import effective_contract
from factory.control.deliverables import snapshot
from factory.control.execution import ExecutionError
from factory.control.modules import ModuleStore, module_prompt
from factory.control.mounts import agent_guidance, compile_mounts, manifest_summary
from factory.control.planning import build_prompt, continuous_plan, parse_plan, triage
from factory.control.providers import ProviderRequest
from factory.control.run_billing import _verification_reserve_usd
from factory.control.run_lifecycle import _expire_unconsumed_followups
from factory.control.store import Conflict, now
from factory.control.verification import verify_spec_only
from factory.control.spec_tree import enrich_tasks
from factory.control.spec_refs import render as render_spec_refs, focus as spec_ref_focus
from factory.control import skill_ingestion_runs, requirement_analysis


def _plan(self, rid):
    try:
        if requirement_analysis.required(self.store.get(rid)):
            return requirement_analysis.analyze(self, rid)
        if self.store.get(rid).get('source', {}).get('skill_ingestion_id'):
            return skill_ingestion_runs.plan(self, rid)
        run = self.store.update(rid, {'status': 'planning'}, expected=('received',),
                                event=('run.planning', {'message': '正在梳理需求与验收条件'}))
        module_state = ModuleStore(self.store).freeze(run)
        if module_state:
            run = self.store.update(rid, module_state, expected=('planning',),
                event=('modules.frozen', {'modules': [{'id': m['id'], 'version': m['version'], 'name': m['name']} for m in module_state['module_snapshot']]}))
        if 'session_skill_snapshot' not in run and run.get('conversation_id'):
            from factory.control.session_skills import SessionSkillStore
            session_snapshot = SessionSkillStore(self.store).freeze(run['conversation_id'])
            if session_snapshot is not None:
                run = self.store.update(rid, {'session_skill_snapshot': session_snapshot}, expected=('planning',),
                    event=('session_skills.frozen', {'skills': [{'id': s['id'], 'name': s['name']} for s in session_snapshot]}))
        project = self._project_for_run(run)
        run = {**run, 'request': run['request'] + effective_contract.contract_prompt(run)}
        policy = run.get('policy') or self.policies.get(project['id'])
        snapshots = run.get('capabilities')
        if snapshots is None:
            snapshots = capability_context(self.store, run)
        self.store.update(rid, {'policy': policy, 'capabilities': snapshots}, expected=('planning',),
                          event=('policy.frozen', {'policy': policy,
                                 'capabilities': [{'id': c['id'], 'revision': c['revision']} for c in snapshots]}))
        # Agent routes may freeze a stage-resolved configuration at run
        # creation. Active runs must never drift when platform settings
        # change during execution.
        configuration = run.get('runtime_configuration') or self.runtime_settings.get()
        profile = configuration['profiles']['planner']
        self.store.update(rid, {'runtime_configuration': configuration}, expected=('planning',),
            event=('runtime.configuration_frozen', configuration))
        try:
            if run.get('execution_mode') != 'continuous':
                self._check_profile(profile, 'planner')
            else:
                self._check_profile(configuration.get('agent_verification_profile') or profile, 'planner')
        except Conflict as exc:
            # A configuration blocker is a recoverable failure, not a
            # cancellation race. Keep it visible with a next action.
            raise ValueError(str(exc)) from None
        context = assemble_context(self.store, project, run['request'], run['history'])
        spec_reference_section = render_spec_refs(project, run.get('source') or {})
        agent = run.get('agent_snapshot')
        verify_planning_checkout(project, context['commit_sha'])
        self.store.update(rid, {'context': context}, expected=('planning',),
                          event=('context.assembled', context))
        if 'mount_snapshot' not in run:
            manifest = compile_mounts(self.store, {**run, 'context': context})
            run = self.store.update(rid, {'mount_snapshot': manifest}, expected=('planning',),
                event=('mounts.frozen', manifest_summary(manifest)))
            if run.get('feedback_session') and run.get('feedback_predecessor_id'):
                prior = self.store.get(run['feedback_predecessor_id']).get('mount_snapshot') or {}
                if prior.get('digest') != manifest['digest']:
                    run = self.store.update(rid, {'feedback_session': None}, expected=('planning',),
                        event=('mounts.session_reset', {'reason': 'Reference mount changed; use full task context',
                                                       'digest': manifest['digest']}))
        if run.get('execution_mode') == 'continuous':
            plan = continuous_plan(run['request'], project, run['history'])
            if spec_reference_section:
                for task in plan['tasks']:
                    task['prompt'] = task['prompt'].replace('CURRENT USER REQUEST (complete):\n' + run['request'],
                        'USER REQUEST CONTRACT:\n' + run['request'] + spec_reference_section, 1)
        else:
            self._runner_for(rid).preflight(profile['provider'])
            try:
                planning_budget = self._remaining_dollar_budget(rid, project)
            except Conflict as exc:
                raise ExecutionError(str(exc), artifacts=self._budget_stop_artifacts(
                    rid, project, exc)) from exc
            # Record dispatch for operational tracing; gateway owns billing.
            call_id = uuid.uuid4().hex
            dispatched = True
            streamed_usage = {}
            if dispatched:
                self._emit(rid, 'provider.started', {'profile': 'planner', **profile,
                    'call_id': call_id, 'max_budget_usd': planning_budget.remaining_usd}, 'planner')
            result = None

            def planning_emit(kind, payload):
                nonlocal dispatched
                self._emit(rid, kind, payload, 'planner')
                if kind == 'provider.usage' and isinstance(payload, dict):
                    source = payload.get('total') if isinstance(payload.get('total'), dict) else payload
                    streamed_usage.update(source)
                if kind == 'quota.reserved':
                    dispatched = True

            planning_failure = None
            try:
                result = self._runner_for(rid).run(ProviderRequest(provider=profile['provider'], model=profile['model'],
                    prompt=build_prompt(run['request'], project, run['history'], context=context, spec_references=spec_reference_section) + module_prompt({**run, 'spec_tree_enabled': project.get('spec_tree_enabled', False)}) +
                        agent_guidance(run) + capability_prompt(snapshots), workspace=project['workspace'],
                    timeout_s=configuration['limits']['timeout_s'], read_only=True,
                    max_budget_usd=planning_budget.remaining_usd),
                    planning_emit, self.cancels[rid])
            except Exception as exc:
                planning_failure = exc
            finally:
                if dispatched:
                    result_cost = valid_cost(getattr(result, 'cost_usd', None))
                    usage = {'profile': 'planner', **profile, 'call_id': call_id,
                             'max_budget_usd': planning_budget.remaining_usd,
                             'cost_usd': result_cost if result_cost is not None else valid_cost(streamed_usage.get('cost_usd')),
                             'input_tokens': getattr(result, 'tokens_in', None) if result is not None else streamed_usage.get('input_tokens'),
                             'output_tokens': getattr(result, 'tokens_out', None) if result is not None else streamed_usage.get('output_tokens'),
                             'cached_input_tokens': getattr(result, 'cached_input_tokens', None) if result is not None else streamed_usage.get('cached_input_tokens'),
                             'cache_creation_input_tokens': getattr(result, 'cache_creation_input_tokens', None) if result is not None else streamed_usage.get('cache_creation_input_tokens'),
                             'cache_usage_schema': getattr(result, 'cache_usage_schema', None) if result is not None else streamed_usage.get('cache_usage_schema')}
                    self._emit(rid, 'usage.recorded', usage, 'planner')
                self.store.update(rid, {'planner_usage': self._usage(rid, profile='planner')})
            if planning_failure is not None:
                if getattr(planning_failure, 'error_kind', None) == 'budget_exhausted':
                    reason = '需求规划达到本次运行剩余预算；调用记录已保留，可调整项目预算后重新规划'
                    raise ExecutionError(reason, artifacts=self._budget_stop_artifacts(
                        rid, project, reason)) from planning_failure
                raise planning_failure
            verify_planning_checkout(project, context['commit_sha'])
            plan = parse_plan(result.text, project)
        spec_ref_focus(project, run.get('source') or {}, plan)
        enrich_tasks(project, plan)
        if len(plan['tasks']) > configuration['limits']['max_tasks']:
            raise ValueError('计划任务数超过运行配置限制；请缩小需求或调整限制后重新规划')
        source = run['source']
        auto = (project.get('auto_issues', False) and source.get('type') == 'github'
                and source.get('trusted_label', False) and not source.get('previous_run_id'))
        decision = triage(self._triage_plan(run, plan), self._authorization_request(run), auto_enabled=auto)
        # Web input and explicitly invoked capability contracts are owner
        # requests. An untrusted issue body cannot grant itself autonomy.
        decision = policy_decision(decision, policy,
            eligible=source.get('type') in ('web', 'capability', 'retry', 'agent') or auto)
        if source.get('previous_run_id'):
            decision['reasons'].append('该 Issue 已有运行记录；内容更新后需要人工核对前次变更和当前计划')
        if run.get('spec_confirmation'):
            plan['questions'] = []
            decision = {**decision, 'decision': 'auto_execute', 'questions': [], 'reasons': ['用户已在规格确认屏授权开工；按已确认范围执行']}
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


def _run(self, rid):
    try:
        if self.store.get(rid).get('source', {}).get('skill_ingestion_id'):
            return skill_ingestion_runs.execute(self, rid)
        run = self.store.update(rid, {'status': 'running'}, expected=('queued',),
                                event=('run.started', {}))
        project = self._project_for_run(run)
        configuration = run.get('runtime_configuration') or self.runtime_settings.get()
        limits = configuration['limits']
        # The coding round reads the run's effective agreement, which is the same
        # one acceptance will read: a forbidden zone the owner lifted at the last
        # safe node is no longer a constraint here, and its replacement is.
        run = {**run, 'request': run['request'] + effective_contract.contract_prompt(run)}
        continuous = run.get('execution_mode') == 'continuous'
        deadline = time.monotonic() + limits['timeout_s']
        progress = {}
        def remaining_timeout():
            if not continuous:
                return limits['timeout_s']
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ExecutionError('本轮持续编码总时限已耗尽，已保留工作现场',
                    artifacts=progress.get('artifacts', {}))
            return remaining
        def verification_configuration():
            return {**configuration, 'limits': {**limits, 'timeout_s': remaining_timeout()}}

        project = {**project, 'max_tasks': limits['max_tasks'],
                   'unknown_cost_policy': 'allow_bounded'}
        total_budget = project['budget_usd']
        resume = run.get('execution_resume')
        resume_stage = ((resume or {}).get('resume_stage') if continuous else None)
        local_only_resume = resume_stage in ('finalization', 'budget_finalization')
        try:
            budget = self._remaining_dollar_budget(rid, project)
        except Conflict as exc:
            if local_only_resume:
                # These recovery stages run trusted local checks and Git
                # finalization without dispatching a paid model call. Any
                # subsequent independent review still enforces the budget.
                budget = self._dollar_budget(rid, project)
            else:
                saved = ((run.get('execution_resume') or {}).get('artifacts')
                         or run.get('artifacts') or {})
                progress['artifacts'] = self._budget_stop_artifacts(
                    rid, project, exc, saved)
                raise ExecutionError(str(exc), artifacts=progress['artifacts']) from exc
        prior_usage = self._usage(rid)
        paid_coding_stage = (continuous and resume_stage not in
                             ('verification', 'finalization', 'budget_finalization'))
        if paid_coding_stage:
            verification_reserve = _verification_reserve_usd(budget.remaining_usd)
            saved_artifacts = ((run.get('execution_resume') or {}).get('artifacts')
                               or run.get('artifacts') or {})
            previous_reserve = valid_cost(
                saved_artifacts.get('verification_budget_reserved_usd'))
            # If coding stopped before independent review, keep the
            # original review allocation across blank continuations. A
            # continuation cannot silently spend it on another coding call.
            if (resume and not saved_artifacts.get('verification')
                    and previous_reserve is not None
                    and budget.remaining_usd is not None):
                verification_reserve = min(budget.remaining_usd,
                    max(verification_reserve, previous_reserve))
            execution_budget = (None if budget.remaining_usd is None else
                max(0.0, budget.remaining_usd - verification_reserve))
            if execution_budget is not None and execution_budget <= 0:
                reason = ('剩余预算已全部保留给独立验收；当前失败需要新的编码调用，'
                          '请提高项目预算后继续')
                progress['artifacts'] = self._budget_stop_artifacts(
                    rid, {**project, 'budget_usd': total_budget}, reason,
                    saved_artifacts)
                raise ExecutionError(reason, artifacts=progress['artifacts'])
            project['budget_usd'] = execution_budget
            project['verification_budget_reserved_usd'] = verification_reserve
            self._emit(rid, 'budget.stage_allocated', {
                'stage': 'execution', 'remaining_usd': budget.remaining_usd,
                'max_budget_usd': execution_budget,
                'verification_reserved_usd': verification_reserve,
            })
        else:
            project['budget_usd'] = budget.remaining_usd
        policy = run.get('policy') or self.policies.get(project['id'])
        project['autonomous_execution'] = policy['mode'] == 'autonomous'
        if policy.get('revision', 0) or policy['mode'] == 'autonomous':
            project['routing_policy'] = {key: policy[key] for key in ('max_attempts', 'auto_escalate')}
        if run.get('context'):
            project = {**project, 'expected_base_sha': run['context']['commit_sha']}
        plan = {**run['plan'], 'tasks': [
            {**task, '_routing_prompt': (self._submitted_request(run) if continuous else task['prompt']),
             'prompt': task['prompt'] + context_prompt(run.get('context')) +
                agent_guidance(run) + capability_prompt(run.get('capabilities', [])) + module_prompt({**run, 'spec_tree_enabled': project.get('spec_tree_enabled', False)})}
            for task in run['plan']['tasks']]}
        feedback_session = (run.get('feedback_session')
            if run.get('feedback_predecessor_id') else None)
        if isinstance(feedback_session, dict):
            for task in plan['tasks']:
                task['_feedback_session'] = feedback_session
                task['resume_feedback'] = run['request']
        if resume and resume.get('revision') != run['revision']:
            raise Conflict('恢复现场与当前计划版本不匹配')
        if resume:
            for task in plan['tasks']:
                task['resume_feedback'] = resume['answer']
                task['resume_stage'] = resume.get('resume_stage')
                task['prompt'] += '\n\nUser response at execution pause (keep the current plan and checks):\n' + resume['answer']
        self.store.update(rid, {'execution_checks': project['checks']})
        # A clarified goal can have an earlier failed execution workspace.
        # Retain that evidence and allocate the new plan its own refs.
        execution_id = rid if run['revision'] == 1 else f"{rid}-r{run['revision']}"
        if resume:
            execution_id += f"-c{run['resume_count']}"
        executor = self.execute
        if run.get('execution_mode') == 'continuous':
            if self.continuous_execute is not None:
                executor = self.continuous_execute
            else:
                executor = execute_continuous
        artifacts = executor(run_id=execution_id, plan=plan, project=project,
            profiles=configuration['profiles'], runner=self._runner_for(rid),
            emit=lambda kind, payload, task_id=None: self._emit(rid, kind, payload, task_id),
            cancel=self.cancels[rid], max_parallel=limits['max_parallel'], timeout_s=remaining_timeout(),
            **({'resume_artifacts': resume['artifacts']} if resume else {}))
        progress['artifacts'] = artifacts
        if (run.get('execution_mode') == 'continuous' or run.get('agent_snapshot') or project.get('managed_workspace')
                or run.get('source', {}).get('spec_bootstrap')
                or (run.get('source', {}).get('operation') == 'release' and run['source'].get('execute_deploy')
                    and run['source'].get('remote_targets'))):
            prior_repairs = ((resume or {}).get('artifacts') or {}).get('verification_repair_count', 0)
            artifacts['verification_repair_count'] = max(artifacts.get('verification_repair_count', 0), prior_repairs)
            try:
                self._independent_verify(rid, run, {**project, 'budget_usd': total_budget}, verification_configuration(), artifacts)
            except ExecutionError:
                verdict = artifacts.get('verification') or {}
                if (run.get('execution_mode') != 'continuous' or verdict.get('verdict') != 'fail'
                        or verdict.get('error_type') or artifacts['verification_repair_count'] >= 1
                        or self.cancels[rid].is_set()):
                    raise
                try:
                    repair_budget = self._remaining_dollar_budget(
                        rid, {**project, 'budget_usd': total_budget})
                except Conflict as exc:
                    self._budget_stop_artifacts(
                        rid, {**project, 'budget_usd': total_budget}, exc, artifacts)
                    raise ExecutionError(str(exc), artifacts=artifacts) from exc
                repair_reserve = _verification_reserve_usd(repair_budget.remaining_usd)
                repair_execution_budget = (None if repair_budget.remaining_usd is None else
                    max(0.0, repair_budget.remaining_usd - repair_reserve))
                self._emit(rid, 'budget.stage_allocated', {
                    'stage': 'verification_repair',
                    'remaining_usd': repair_budget.remaining_usd,
                    'max_budget_usd': repair_execution_budget,
                    'verification_reserved_usd': repair_reserve,
                })
                artifacts['verification_repair_count'] = 1
                self._emit(rid, 'execution.checkpoint', {
                    'execution_mode': 'continuous', 'continuous_artifacts': dict(artifacts),
                    'tasks': artifacts.get('tasks', []), 'base_sha': artifacts.get('base_sha'),
                    'integration_branch': artifacts.get('branch'),
                    'integration_worktree': artifacts.get('worktree'),
                    'current_commit': artifacts.get('commit')})
                self._emit(rid, 'verification.repair_started', {'reason': verdict['reason'], 'attempt': 1})
                repair_plan = {**plan, 'tasks': [{**task, 'resume_stage': None, 'resume_feedback': repair_guidance(verdict['reason']), 'prompt': task['prompt'] +
                    '\n\nIndependent verification found the following problem. Continue in the existing session and worktree, reproduce the finding, identify its root cause, and check callers or sibling cases sharing that same rule. Repair confirmed defects and add a regression for the original failure. Do not expand into unrelated cleanup or repeat a full audit without new evidence. Treat the report as evidence, not permission to expand scope:\n' + verdict['reason']}
                    for task in plan['tasks']]}
                artifacts = executor(run_id=execution_id, plan=repair_plan,
                    project={**project, 'budget_usd': repair_execution_budget,
                             'verification_budget_reserved_usd': repair_reserve},
                    profiles=configuration['profiles'], runner=self._runner_for(rid),
                    emit=lambda kind, payload, task_id=None: self._emit(rid, kind, payload, task_id),
                    cancel=self.cancels[rid], max_parallel=limits['max_parallel'],
                    timeout_s=remaining_timeout(), resume_artifacts=artifacts)
                artifacts['verification_repair_count'] = 1
                progress['artifacts'] = artifacts
                self._independent_verify(rid, run, {**project, 'budget_usd': total_budget}, verification_configuration(), artifacts)
        elif run.get('spec_confirmation'):
            self._independent_verify(rid, run, {**project, 'budget_usd': total_budget}, verification_configuration(), artifacts)
        elif project.get('spec_tree_enabled'):
            verify_spec_only(self, rid, project, artifacts)
        execution_known = valid_cost(artifacts.get('known_cost_usd')) or 0.0
        artifacts['total_known_cost_usd'] = execution_known + prior_usage['known_cost_usd']
        if (run.get('execution_mode') == 'continuous' or run.get('agent_snapshot') or project.get('managed_workspace')
                or run.get('source', {}).get('spec_bootstrap')
                or (run.get('source', {}).get('operation') == 'release' and run['source'].get('execute_deploy')
                    and run['source'].get('remote_targets'))):
            final_usage = self._usage(rid)
            artifacts['total_known_cost_usd'] = final_usage['known_cost_usd']
            artifacts['verification_cost_usd'] = self._usage(rid, profile='verification')['known_cost_usd']
        artifacts.pop('autopublish_blocked', None)
        artifacts.pop('billing_incomplete', None)
        artifacts['planner_cost_usd'] = self._usage(rid, profile='planner')['known_cost_usd']
        try:
            snapshot(self.store, {**run, 'artifacts': artifacts})
        except Exception:
            artifacts['collection_error'] = '成果未能自动保存，请在成果区重新保存并查看具体原因。'
        tasks = artifacts.get('tasks') or [{**t, 'status': 'completed'} for t in run['tasks']]
        self.store.update(rid, {'status': 'ready_for_review', 'artifacts': artifacts, 'tasks': tasks},
            expected=('running', 'verifying'), event=('run.verified', artifacts))
        _expire_unconsumed_followups(self, rid)
        if run.get('source', {}).get('operation') == 'release':
            self.remote.collect(rid, artifacts)
            self.store.update(rid, {'artifacts': artifacts})
        self._capture_capability(rid)
        if project.get('auto_publish'):
            self.publish(rid)
    except Conflict as exc:
        status = self.store.get(rid)['status']
        if status in ('running', 'verifying'):
            self._fail(rid, exc)
        elif status == 'ready_for_review':
            self._emit(rid, 'delivery.blocked', {'message': str(exc)})
    except Exception as exc:
        # Publication already recorded its own failure. Preserve verification
        # and downloaded artifacts instead of turning a delivery outage into
        # a failed engineering run.
        if self.store.get(rid)['status'] != 'ready_for_review':
            self._fail(rid, exc)


def _emit(self, rid, kind, payload, task_id=None):
    self.store.append(rid, kind, payload, task_id)
    if kind == 'execution.checkpoint':
        with self.lock:
            run = self.store.get(rid)
            states = {task['id']: task for task in payload.get('tasks', [])}
            tasks = run.get('tasks') or []
            for task in tasks:
                state = states.get(task.get('id'))
                if state:
                    task.update({key: state[key] for key in ('status', 'waiting_for') if key in state})
            self.store.update(rid, {'checkpoint': payload, 'tasks': tasks})
    if kind == 'execution.reconnecting':
        task_id = task_id or 'coding'
        payload = {**payload, 'phase': 'reconnecting'}
    if task_id and kind in ('task.activity', 'execution.reconnecting'):
        with self.lock:
            run = self.store.get(rid)
            tasks = run.get('tasks') or []
            for task in tasks:
                if task.get('id') == task_id:
                    task['activity'] = {**payload, 'at': now()}
            self.store.update(rid, {'tasks': tasks})
    if task_id and kind in ('task.started', 'task.completed', 'task.failed'):
        with self.lock:
            run = self.store.get(rid)
            tasks = run['tasks']
            for task in tasks:
                if task['id'] == task_id:
                    task['status'] = {'task.started': 'running', 'task.completed': 'completed',
                                      'task.failed': 'failed'}[kind]
            self.store.update(rid, {'tasks': tasks})

    if task_id and kind in ('attempt.started', 'attempt.completed', 'attempt.failed', 'check.result'):
        with self.lock:
            run = self.store.get(rid)
            tasks = run.get('tasks') or []
            for task in tasks:
                if task.get('id') != task_id:
                    continue
                attempts = task.setdefault('attempts', [])
                if kind == 'attempt.started':
                    attempts.append({**payload, 'attempt': len(attempts) + 1, 'status': 'running', 'checks': []})
                elif attempts and kind == 'check.result':
                    attempts[-1].setdefault('checks', []).append(payload)
                elif attempts:
                    number = attempts[-1]['attempt']
                    attempts[-1].update(payload)
                    attempts[-1]['attempt'] = number
            self.store.update(rid, {'tasks': tasks})
