"""Pure projection for the operations overview's engineering lifecycle."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from urllib.parse import quote


STAGES = (
    ("intake", "需求澄清", "已接收或等待补充的信息。", {"received", "needs_clarification"}),
    ("plan", "方案规划", "正在规划或等待批准的运行。", {"planning", "awaiting_approval"}),
    ("build", "开发执行", "已排队或正在执行的任务。", {"queued", "running"}),
    ("verify", "质量验证", "正在核验交付证据的运行。", {"verifying"}),
    ("deliver", "交付发布", "检查已通过，待发布、发布中或已发布的运行。", {"ready_for_review", "publishing", "published"}),
)

STATUS_DETAILS = {
    "received": "已接收",
    "needs_clarification": "等待补充信息",
    "planning": "正在规划",
    "awaiting_approval": "等待批准",
    "queued": "已排队",
    "running": "正在执行",
    "verifying": "正在验证",
    "ready_for_review": "已验证，待发布",
    "publishing": "正在发布",
    "published": "已发布",
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
    """Keep one latest supplied revision for each capability id.

    CapabilityStore.list() already returns one current version, but this makes the
    projection safe for callers that provide a version history.
    """
    latest: dict[str, Mapping] = {}
    for capability in capabilities:
        raw_id = capability.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            continue
        prior = latest.get(raw_id)
        revision = capability.get("revision")
        prior_revision = prior.get("revision") if prior else None
        if prior is None or (isinstance(revision, int) and (not isinstance(prior_revision, int) or revision >= prior_revision)):
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


def engineering_overview(runs: Iterable[Mapping], capabilities: Iterable[Mapping], project_names: Mapping[object, str]) -> dict:
    """Return a factual six-stage lifecycle without inferring progress.

    `runs` and `capabilities` are accepted as mappings so this remains independently
    testable and does not acquire database state or alter persisted records.
    """
    run_list = [run for run in runs if isinstance(run, Mapping)]
    current_capabilities = _current_capabilities(capability for capability in capabilities if isinstance(capability, Mapping))
    grouped: dict[str, list[Mapping]] = {stage_id: [] for stage_id, _, _, _ in STAGES}
    for run in run_list:
        status = run.get("status")
        for stage_id, _, _, states in STAGES:
            if status in states:
                grouped[stage_id].append(run)
                break

    stages = []
    for stage_id, label, description, _ in STAGES:
        members = sorted(grouped[stage_id], key=_updated, reverse=True)
        stages.append({
            "id": stage_id,
            "label": label,
            "description": description,
            "count": len(members),
            "unit": "次运行",
            "items": [{
                "id": str(run.get("id", "")),
                "title": _run_title(run),
                "project_name": project_names.get(run.get("project_id"), "项目"),
                "status": run.get("status"),
                "updated_at": run.get("updated_at", run.get("created_at")),
                "href": f"/runs/{quote(str(run.get('id', '')), safe='')}",
                "detail": STATUS_DETAILS.get(run.get("status"), "已记录状态"),
            } for run in members[:6]],
        })

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
            "project_name": project_names.get(next((run.get("project_id") for run in run_list
                                                       if run.get("id") == capability.get("source_run_id")), None), "来源运行未保留"),
            "status": capability.get("status"),
            "updated_at": capability.get("updated_at", capability.get("created_at")),
            "href": f"/capabilities?selected={quote(capability['id'], safe='')}",
            "detail": "能力草稿" if capability.get("status") == "draft" else "可调用能力" if capability.get("status") == "ready" else "已记录能力状态",
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
