"""Explicit one-click budget renewal reuses the existing checkpoint continuation."""
from factory.control.store import Conflict
from factory.control.requirement_analysis import required


def resume(self, rid, revision, resume_count, actor):
    with self.lock:
        run = self.store.get(rid)
        if (run['status'] != 'needs_human' or run['revision'] != revision
                or run.get('resume_count', 0) != resume_count or rid in self.active_jobs):
            raise Conflict('运行已变化或仍在保存，请刷新后续跑')
        project = self.store.project(run['project_id'])
        if required(run) and not run.get('plan'):
            limit = project.get('requirement_analysis_budget_usd', 5.0)
            self.store.update(rid, {'requirement_analysis_credit_usd': run.get('requirement_analysis_credit_usd', 0) + (limit or 0),
                'status': 'received', 'error': None, 'resume_count': resume_count + 1},
                expected=('needs_human',), event=('budget.renewed', {'actor': actor, 'phase': 'requirement_analysis', 'additional_usd': limit}))
            self.start_plan(rid)
            return self.store.get(rid)
        artifacts = run.get('artifacts') or {}
        if not artifacts.get('budget_stop') and not artifacts.get('budget_exhausted'):
            raise Conflict('当前不是预算暂停，请使用现有继续操作')
        credit = run.get('budget_credit_usd', 0)
        extra = project.get('budget_usd') or 0
        self.store.update(rid, {'budget_credit_usd': credit + extra})
        try:
            result = self.continue_run(rid, '', revision, resume_count, actor)
        except Exception:
            self.store.update(rid, {'budget_credit_usd': credit})
            raise
        self._emit(rid, 'budget.renewed', {'actor': actor, 'phase': 'coding_verification',
            'additional_usd': extra, 'resume_stage': (result.get('execution_resume') or {}).get('resume_stage') or 'coding'})
        return result
