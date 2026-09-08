"""Application coordinator; all durable transitions are guarded and auditable."""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

from factory.control.store import ACTIVE, Conflict, Store


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
        self.store = store
        self.runner = runner or SDKRunner()
        self.publisher = publisher
        self.profiles = profiles or configured_profiles()
        self.execute = execute or execute_plan
        self.timeout_s = timeout_s
        self.max_parallel = max_parallel
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='factory-control')
        self.cancels = {}
        self.lock = threading.RLock()
        self.futures = set()

    def _submit(self, fn, rid):
        with self.lock:
            self.futures = {f for f in self.futures if not f.done()}
            if len(self.futures) >= 16:
                raise Conflict('当前任务已满，请稍后重试')
            self.cancels[rid] = threading.Event()
            future = self.pool.submit(fn, rid)
            self.futures.add(future)

    def start_plan(self, rid):
        self._submit(self._plan, rid)

    def _emit(self, rid, kind, payload, task_id=None):
        self.store.append(rid, kind, payload, task_id)
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
                changes['artifacts'] = exc.artifacts
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
            profile = self.profiles['planner']
            if not profile.get('model'):
                raise ValueError('请配置 FACTORY_PLANNER_PROVIDER 和 FACTORY_PLANNER_MODEL，再补充需求重试')
            self._emit(rid, 'provider.started', {'profile': 'planner', **profile}, 'planner')
            result = self.runner.run(ProviderRequest(provider=profile['provider'], model=profile['model'],
                prompt=build_prompt(run['request'], project, run['history']), workspace=project['workspace'],
                timeout_s=self.timeout_s, read_only=True),
                lambda kind, payload: self._emit(rid, kind, payload, 'planner'), self.cancels[rid])
            plan = parse_plan(result.text, project)
            source = run['source']
            auto = (project.get('auto_issues', False) and source.get('type') == 'github'
                    and source.get('trusted_label', False) and not source.get('previous_run_id'))
            decision = triage(plan, run['request'], auto_enabled=auto)
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
        except Conflict:
            pass  # Cancellation won the transition.
        except Exception as exc:
            self._fail(rid, exc)

    def clarify(self, rid, answer, actor):
        with self.lock:
            run = self.store.get(rid)
            updated = self.store.update(rid, {'status': 'received', 'plan': None, 'triage': None,
                'history': [*run['history'], answer], 'tasks': []},
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
            for task in run['plan']['tasks']:
                if not task['acceptance'] or not task['paths'] or not task['checks']:
                    raise Conflict('任务缺少验收标准、修改范围或已配置检查')
                if any(c not in project['checks'] for c in task['checks']):
                    raise Conflict('检查配置已变化，请重新规划')
            updated = self.store.update(rid, {'status': 'queued'}, expected=('awaiting_approval',),
                revision=revision, event=('human.approved', {'actor': actor, 'revision': revision}))
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
            artifacts = self.execute(run_id=rid, plan=run['plan'], project=project,
                profiles=self.profiles, runner=self.runner,
                emit=lambda kind, payload, task_id=None: self._emit(rid, kind, payload, task_id),
                cancel=self.cancels[rid], max_parallel=self.max_parallel, timeout_s=self.timeout_s)
            tasks = artifacts.get('tasks') or [{**t, 'status': 'completed'} for t in run['tasks']]
            self.store.update(rid, {'status': 'ready_for_review', 'artifacts': artifacts, 'tasks': tasks},
                expected=('running', 'verifying'), event=('run.verified', artifacts))
            if project.get('auto_publish') and not artifacts.get('autopublish_blocked') and not artifacts.get('billing_incomplete'):
                self.publish(rid)
        except Conflict:
            pass
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
                expected=('received', 'planning', 'queued', 'running', 'verifying', 'awaiting_approval', 'needs_clarification'),
                event=('run.cancelled', {'message': '用户取消执行，保留日志和工作区', 'actor': actor}))

    def close(self):
        for event in self.cancels.values():
            event.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        if self.publisher and hasattr(self.publisher, 'close'):
            self.publisher.close()
