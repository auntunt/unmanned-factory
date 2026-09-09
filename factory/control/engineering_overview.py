"""Pure projection for the operations overview's engineering lifecycle.

The lifecycle is an evidence projection, rather than a current-status bucket.
Current snapshot evidence determines stage counts. Historical events remain
available in the audit trail but do not advance a newer plan.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from urllib.parse import quote


STAGES = (
    ("intake", "需求澄清", "已接收的需求与补充信息。"),
    ("plan", "方案规划", "已经形成或正在形成的执行方案。"),
    ("build", "开发执行", "已排队、执行或留下任务尝试记录。"),
    ("verify", "质量验证", "已经产生检查结果或进入验证阶段。"),
    ("deliver", "交付发布", "已有提交、PR 或发布状态记录。"),
)

STATUS_DETAILS = {
    "received": "已接收",
    "needs_clarification": "等待补充信息",
    "planning": "正在规划",
    "awaiting_approval": "等待批准",
    "queued": "已排队",
    "running": "正在执行",
    "verifying": "正在验证",
    "ready_for_review": "已验证，可查看成果",
    "publishing": "正在发布",
    "published": "已发布",
    "needs_human": "等待人工处理",
    "failed": "运行失败",
    "cancelled": "已取消",
}


def _updated(item: Mapping) -> str:
    return str(item.get("updated_at") or item.get("created_at") or "")


def _run_title(run: Mapping) -> str:
    plan = run.get("plan")
    if isinstance(plan, Mapping) and isinstance(plan.get("title"), str) and plan["title"].strip():
        return plan["title"].strip()
    request = run.get("request")
    return request.strip() if isinstance(request, str) and request.strip() else "未命名运行"


def _current_capabilities(capabilities: Iterable[Mapping]) -> list[Mapping]:
    """Keep one latest supplied revision for each capability id."""
    latest: dict[str, Mapping] = {}
    for capability in capabilities:
        raw_id = capability.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            continue
        prior = latest.get(raw_id)
        revision = capability.get("revision")
        prior_revision = prior.get("revision") if prior else None
        if prior is None or (isinstance(revision, int) and
                             (not isinstance(prior_revision, int) or revision >= prior_revision)):
            latest[raw_id] = capability
    return list(latest.values())


def _frozen_source_run_ids(run: Mapping) -> set[str]:
    snapshots: list[object] = [run.get("capability")]
    capabilities = run.get("capabilities")
    if isinstance(capabilities, list):
        snapshots.extend(capabilities)
    return {
        source_run_id
        for snapshot in snapshots
        if isinstance(snapshot, Mapping)
        for source_run_id in [snapshot.get("source_run_id")]
        if isinstance(source_run_id, str) and source_run_id
    }


def _event_map(events: Mapping[str, Iterable[Mapping]] | Iterable[Mapping] | None) -> dict[str, list[Mapping]]:
    if events is None:
        return {}
    if isinstance(events, Mapping):
        return {str(rid): [event for event in values if isinstance(event, Mapping)]
                for rid, values in events.items()}
    result: dict[str, list[Mapping]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        rid = event.get("run_id")
        if rid is not None:
            result.setdefault(str(rid), []).append(event)
    return result


def _has_plan(run: Mapping, events: list[Mapping]) -> bool:
    plan = run.get("plan")
    return (isinstance(plan, Mapping) and bool(plan)) or any(
        event.get("type") == "plan.created" for event in events
    ) or run.get("status") in {"planning", "awaiting_approval"}


def _has_execution_tasks(value) -> bool:
    if not isinstance(value, list):
        return False
    for task in value:
        if not isinstance(task, Mapping):
            continue
        attempts = task.get("attempts")
        if isinstance(attempts, list) and attempts:
            return True
        if task.get("status") in {"running", "completed", "verified", "failed", "cancelled"}:
            return True
    return False


def _has_attempt_checks(value) -> bool:
    if not isinstance(value, list):
        return False
    for task in value:
        if not isinstance(task, Mapping):
            continue
        attempts = task.get("attempts")
        if not isinstance(attempts, list):
            continue
        if any(isinstance(attempt, Mapping) and bool(attempt.get("checks")) for attempt in attempts):
            return True
    return False


def _has_checks_in_attempts(value) -> bool:
    return isinstance(value, list) and any(
        isinstance(attempt, Mapping) and bool(attempt.get("checks")) for attempt in value
    )


def _has_build(run: Mapping, events: list[Mapping]) -> bool:
    tasks = run.get("tasks")
    artifacts = run.get("artifacts")
    artifact_tasks = artifacts.get("tasks") if isinstance(artifacts, Mapping) else None
    return _has_execution_tasks(tasks) or _has_execution_tasks(artifact_tasks) or any(
        event.get("type") in {"run.started", "task.started", "task.completed", "task.failed", "execution.checkpoint"}
        for event in events
    ) or run.get("status") in {"queued", "running", "verifying"}


def current_evidence(run: Mapping) -> dict:
    """One projection of current snapshot facts for every workbench surface."""
    artifacts = run.get('artifacts') if isinstance(run.get('artifacts'), Mapping) else {}
    tasks = run.get('tasks') or artifacts.get('tasks') or []
    checks = list(artifacts.get('checks') or [])
    if not checks:
        for task in tasks:
            if not isinstance(task, Mapping):
                continue
            attempts = task.get('attempts') or []
            latest = attempts[-1] if attempts else {}
            checks.extend((latest.get('checks') if attempts else task.get('checks')) or [])
    checks = [check for check in checks if isinstance(check, Mapping)]
    def passed(check):
        if check.get('timeout') or check.get('cancelled') or check.get('exit') not in (None, 0):
            return False
        outcome = check.get('outcome', check.get('status'))
        return outcome in ('passed', 'pass') if outcome is not None else check.get('exit') == 0
    failure = any(check.get('outcome', check.get('status')) in ('failed', 'fail') or check.get('timeout') or check.get('cancelled') or check.get('exit') not in (None, 0) for check in checks)
    return {'requirements': bool(str(run.get('request') or '').strip()),
            'plan': bool(run.get('plan')), 'execution': _has_execution_tasks(tasks),
            'checks': 'none' if not checks else 'failed' if failure else 'passed' if all(passed(check) for check in checks) else 'recorded',
            'delivery': bool(artifacts.get('commit') or artifacts.get('pr_url'))}


def _has_verify(run: Mapping, events: list[Mapping]) -> bool:
    return current_evidence(run)['checks'] != 'none' or run.get('status') == 'verifying'


def _has_delivery(run: Mapping, events: list[Mapping]) -> bool:
    artifacts = run.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    has_delivery_artifact = bool(artifacts.get("commit") or artifacts.get("pr_url") or
                                 artifacts.get("pr_number") or artifacts.get("merge_evidence"))
    return has_delivery_artifact or run.get("status") in {"ready_for_review", "publishing", "published"} or any(
        event.get("type") in {"github.publish_started", "github.published", "github.publish_failed",
                               "delivery.blocked"}
        for event in events
    )


def _run_item(run: Mapping, project_names: Mapping[object, str], stage_id: str, detail: str | None = None) -> dict:
    rid = str(run.get("id", ""))
    view = {
        "intake": "requirements",
        "plan": "plan",
        "build": "execution",
        "verify": "verification",
        "deliver": "delivery",
    }[stage_id]
    return {
        "id": rid,
        "title": _run_title(run),
        "project_name": project_names.get(run.get("project_id"), "项目"),
        "status": run.get("status"),
        "updated_at": run.get("updated_at", run.get("created_at")),
        "href": f"/runs/{quote(rid, safe='')}?view={view}",
        "detail": detail or STATUS_DETAILS.get(run.get("status"), "已记录状态"),
    }


def engineering_overview(
    runs: Iterable[Mapping],
    capabilities: Iterable[Mapping],
    project_names: Mapping[object, str],
    events: Mapping[str, Iterable[Mapping]] | Iterable[Mapping] | None = None,
) -> dict:
    """Return lifecycle stages from facts already present in the store.

    ``events`` is optional for compatibility with callers and unit tests that
    only have run snapshots. Historical events are retained by callers for
    audit, but current stage counts use the current run snapshot only.
    """
    run_list = [run for run in runs if isinstance(run, Mapping)]
    event_map = _event_map(events)
    current_capabilities = _current_capabilities(
        capability for capability in capabilities if isinstance(capability, Mapping)
    )
    predicates = {
        "intake": lambda run, run_events: bool(run.get("request")) or any(
            event.get("type") == "user.message" for event in run_events),
        "plan": _has_plan,
        "build": _has_build,
        "verify": _has_verify,
        "deliver": _has_delivery,
    }
    grouped: dict[str, list[Mapping]] = {stage_id: [] for stage_id, _, _ in STAGES}
    for run in run_list:
        run_events = event_map.get(str(run.get("id")), [])
        for stage_id, _, _ in STAGES:
            if predicates[stage_id](run, []):
                grouped[stage_id].append(run)

    stages = []
    for stage_id, label, description in STAGES:
        members = sorted(grouped[stage_id], key=_updated, reverse=True)
        stages.append({
            "id": stage_id,
            "label": label,
            "description": description,
            "count": len(members),
            "unit": {"intake": "条需求", "plan": "项规划", "build": "项执行", "verify": "项检查记录", "deliver": "项交付"}[stage_id],
            "items": [_run_item(run, project_names, stage_id) for run in members[:6]],
        })

    run_by_id = {str(run.get("id")): run for run in run_list}
    derived = [capability for capability in current_capabilities
               if isinstance(capability.get("source_run_id"), str) and capability["source_run_id"]]
    derived.sort(key=_updated, reverse=True)
    stages.append({
        "id": "reuse",
        "label": "能力沉淀",
        "description": "由已记录来源运行提炼的能力草稿或可调用能力。",
        "count": len(derived),
        "unit": "项能力",
        "items": [{
            "id": capability["id"],
            "title": capability.get("name") if isinstance(capability.get("name"), str) else "未命名能力",
            "project_name": project_names.get(
                (run_by_id.get(str(capability.get("source_run_id"))) or {}).get("project_id"),
                "来源运行未保留",
            ),
            "status": capability.get("status"),
            "updated_at": capability.get("updated_at", capability.get("created_at")),
            "href": f"/capabilities?selected={quote(capability['id'], safe='')}",
            "detail": "能力草稿" if capability.get("status") == "draft" else
                      "可调用能力" if capability.get("status") == "ready" else "已记录能力状态",
        } for capability in derived[:6]],
    })

    verified_states = {"ready_for_review", "publishing", "published"}
    derived_source_runs = {capability["source_run_id"] for capability in derived}
    return {
        "stages": stages,
        "verified_runs": sum(run.get("status") in verified_states for run in run_list),
        "published_runs": sum(run.get("status") == "published" for run in run_list),
        "distilled_runs": len(derived_source_runs),
        "reused_runs": sum(bool(_frozen_source_run_ids(run)) for run in run_list),
        "draft_capabilities": sum(capability.get("status") == "draft" for capability in current_capabilities),
        "ready_capabilities": sum(capability.get("status") == "ready" for capability in current_capabilities),
    }
