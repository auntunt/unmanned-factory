"""Restart reconciliation and checkpoint stage selection without replaying ambiguous writes."""
from __future__ import annotations

import json
import logging

from factory.control.autonomy import all_events
from factory.control import effective_contract
from factory.control.execution import ExecutionError
from factory.control.store import ACTIVE, scrub


def _has_successful_command_evidence(artifacts):
    """A legacy budget stop may attempt local checks, but never skip them."""
    if not isinstance(artifacts, dict):
        return False
    for task in artifacts.get('tasks') or []:
        batches = [task.get('command_evidence') or []]
        batches.extend(attempt.get('command_evidence') or []
                       for attempt in task.get('attempts') or [])
        if any(record.get('exit_code') == 0 and not record.get('timeout')
               and not record.get('cancelled')
               for batch in batches for record in batch if isinstance(record, dict)):
            return True
    return False


def _failed_platform_checks(artifacts):
    """Return trusted configured checks that did not complete successfully."""
    if not isinstance(artifacts, dict):
        return []
    records = list(artifacts.get('checks') or [])
    checkpoint = artifacts.get('finalization_checkpoint')
    if isinstance(checkpoint, dict):
        records.extend(checkpoint.get('checks') or [])
    return [record for record in records
            if isinstance(record, dict) and (record.get('cancelled')
            or record.get('timeout') or record.get('exit') != 0)]


def _continuous_resume_stage(artifacts, *, budget_stop=False):
    """Choose a safe continuous stage that does not need another coding call."""
    if not isinstance(artifacts, dict):
        return None
    failed_checks = _failed_platform_checks(artifacts)
    if artifacts.get('finalization_checkpoint') and not failed_checks:
        return 'finalization'
    if ((artifacts.get('budget_exhausted') or budget_stop)
            and not artifacts.get('commit')
            and _has_successful_command_evidence(artifacts)
            and not failed_checks):
        # A provider can hit its dollar ceiling after implementation and tests
        # but before returning the final assistant message. Only trusted command
        # evidence permits the platform-owned checks and commit to resume.
        return 'budget_finalization'
    if (artifacts.get('commit') and artifacts.get('tasks')
            and all(task.get('status') == 'verified'
                    for task in artifacts['tasks'])
            and (not artifacts.get('verification')
                 or artifacts['verification'].get('error_type'))):
        return 'verification'
    return None


def _check_failure_context(artifacts):
    failures = _failed_platform_checks(artifacts)
    if not failures:
        return ''
    evidence = [scrub({key: record.get(key) for key in
        ('name', 'argv', 'exit', 'timeout', 'cancelled', 'stdout', 'stderr')})
        for record in failures[:5]]
    return json.dumps(evidence, ensure_ascii=False)[:6000]


def recover(self):
    """Recover unstarted work automatically; preserve ambiguous writes for reconciliation."""
    with self.lock:
        self.queue.acquire()
        # Only a process that took the worker lock is the one taking over, so
        # this is where unacknowledged maintenance turns are retired -- not in
        # ``Service.__init__``, where merely opening the database did it.
        self.recover_maintenance_jobs()
        if self.governance is not None:
            self.governance.recover()
        for run in self.store.all_runs():
            retry_of = run.get('source', {}).get('retry_of')
            if retry_of and run.get('conversation_id') and run['status'] == 'received':
                c = self.agents.conversation(run['conversation_id'])
                if c.get('run_id') == retry_of:
                    self.agents.attach_run(c['id'], run['id'])
            if run['status'] in ('ready_for_review', 'published') and run.get('policy') and not run.get('capability_candidate_id'):
                self._capture_capability(run['id'])
            if run['status'] == 'requirement_analysis':
                self._record_interrupted_provider_usage(run)
                self.store.update(run['id'], {'status': 'needs_human', 'error': '需求分析被重启中断，费用保留，请续跑分析'}, expected=('requirement_analysis',), event=('requirement_analysis.interrupted', {}))
                continue
            if run['status'] not in ACTIVE:
                continue
            rid = run['id']
            self._record_interrupted_provider_usage(run)
            if run.get('source', {}).get('type') == 'skill_ingestion':
                # No source scripts or external writes can occur in this zero-tool workflow.
                from factory.control.skill_ingestion_runs import clean_interrupted_scratch
                clean_interrupted_scratch(self, rid)
                self.queue.reset(rid)
                self.store.update(rid, {'status': 'received'}, expected=(run['status'],),
                    event=('run.resumed', {'phase': 'plan', 'message': '接续适配与独立验收的已保存分片'}))
                self.queue.enqueue(rid, 'plan')
                continue
            try:
                policy = run.get('policy') or self.policies.get(run['project_id'])
            except KeyError:
                policy = {'resume_on_restart': False}
            self.queue.reset(rid)
            if run.get('source', {}).get('type') == 'inspection' and (run['status'] in ('running', 'verifying') or not policy['resume_on_restart']):
                self._fail(rid, ExecutionError('巡检在服务重启时中断；已保留现有证据，等待下一次巡检', artifacts=run.get('artifacts') or {}))
                continue
            if (policy['resume_on_restart'] and run.get('execution_mode') == 'continuous'
                    and run['status'] in ('running', 'verifying')):
                checkpoints = [event['payload'] for event in all_events(self.store, rid)
                    if event['type'] == 'execution.checkpoint' and
                    isinstance(event['payload'].get('continuous_artifacts'), dict)]
                if checkpoints:
                    artifacts = checkpoints[-1]['continuous_artifacts']
                    resume_stage = _continuous_resume_stage(artifacts)
                    # Automatic recovery returns to the same site as the two
                    # interactive resumes, so it binds the agreement the same way.
                    # Without this the restarted round rendered whatever agreement
                    # was current when it started coding again: a supplement applied
                    # by another path, or an edited draft under a re-derived
                    # revision 1, silently became the agreement the recovered
                    # session was judged against.
                    binding = effective_contract.resume_binding(run)
                    resumed = self.store.update(rid, {'status': 'queued', 'artifacts': artifacts,
                        'resume_count': run.get('resume_count', 0) + 1,
                        'execution_resume': {'artifacts': artifacts, 'revision': run['revision'],
                            'answer': '服务重启后接续原有编码会话，保留工作区与已完成成果，继续验证并交付。',
                            'resume_stage': resume_stage, **binding}},
                        expected=(run['status'],), event=('run.resumed',
                            {'phase': 'execute', 'execution_mode': 'continuous',
                             'message': '已恢复持续编码检查点，正在接续原会话', **binding}))
                    self.queue.enqueue(rid, 'execute')
                    continue
            if ((run.get('feedback_predecessor_id') and run['status'] == 'received') or
                    (policy['resume_on_restart'] and run['status'] in ('received', 'planning', 'queued'))):
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
    # Auto-resume needs_human runs that have unconsumed pending followups.
    # Runs after _ensure_scheduler so the durable queue and thread pool are
    # ready to accept _submit.  The requirement_analysis interrupted path
    # (line 89) sets needs_human but uses budget_resume semantics, not
    # continue_run; those runs have no plan or resumable artifacts, so
    # _auto_resume_with_followups correctly skips them.
    from factory.control.run_lifecycle import _auto_resume_with_followups, _collect_pending_followups
    log = logging.getLogger(__name__)
    for run in self.store.all_runs():
        if run['status'] == 'needs_human' and _collect_pending_followups(self, run['id']):
            log.info('recover: auto-resuming %s with pending followups', run['id'])
            _auto_resume_with_followups(self, run['id'])
