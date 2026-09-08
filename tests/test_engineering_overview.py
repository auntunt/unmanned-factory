from factory.control.engineering_overview import engineering_overview


def run(run_id, status, *, updated_at="2026-09-08T01:00:00+00:00", project_id="project"):
    return {"id": run_id, "status": status, "project_id": project_id, "request": f"request {run_id}",
            "updated_at": updated_at}


def by_id(summary):
    return {stage["id"]: stage for stage in summary["stages"]}


def test_engineering_stages_keep_stage_evidence_independent_of_current_status():
    statuses = ("received", "needs_clarification", "planning", "awaiting_approval", "queued", "running",
                "verifying", "ready_for_review", "publishing", "published", "failed", "needs_human", "cancelled")
    summary = engineering_overview([run(status, status) for status in statuses], [], {"project": "工程"})
    stages = by_id(summary)

    assert [stage["id"] for stage in summary["stages"]] == ["intake", "plan", "build", "verify", "deliver", "reuse"]
    assert stages["intake"]["count"] == len(statuses)
    assert stages["plan"]["count"] == 2
    assert stages["build"]["count"] == 3
    assert stages["verify"]["count"] == 1
    assert stages["deliver"]["count"] == 3
    assert stages["intake"]["items"][0]["href"].endswith("?view=requirements")
    assert stages["plan"]["items"][0]["href"].endswith("?view=plan")
    assert stages["build"]["items"][0]["href"].endswith("?view=execution")
    assert stages["verify"]["items"][0]["href"].endswith("?view=verification")
    assert stages["deliver"]["items"][0]["href"].endswith("?view=delivery")
    assert summary["verified_runs"] == 3
    assert summary["published_runs"] == 1


def test_stage_count_remains_complete_when_only_the_six_newest_items_are_returned():
    runs = [run(f"build-{number}", "running", updated_at=f"2026-09-08T{number:02}:00:00+00:00")
            for number in range(7)]
    build = by_id(engineering_overview(runs, [], {"project": "工程"}))["build"]

    assert build["count"] == 7
    assert [item["id"] for item in build["items"]] == ["build-6", "build-5", "build-4", "build-3", "build-2", "build-1"]


def test_templates_are_counted_as_drafts_but_are_not_presented_as_distilled_or_reusable():
    capabilities = [
        {"id": "template", "name": "Built-in template", "status": "draft", "revision": 1},
        {"id": "harvested", "name": "Harvested draft", "status": "draft", "revision": 1,
         "source_run_id": "source", "updated_at": "2026-09-09T01:00:00+00:00"},
        {"id": "ready", "name": "Ready capability", "status": "ready", "revision": 1,
         "source_run_id": "source", "updated_at": "2026-09-10T01:00:00+00:00"},
    ]
    summary = engineering_overview([run("source", "published")], capabilities, {"project": "工程"})
    reuse = by_id(summary)["reuse"]

    assert summary["draft_capabilities"] == 2
    assert summary["ready_capabilities"] == 1
    assert summary["distilled_runs"] == 1
    assert reuse["count"] == 2 and reuse["unit"] == "项能力"
    assert {item["id"] for item in reuse["items"]} == {"harvested", "ready"}
    assert all("模板" not in item["detail"] for item in reuse["items"])


def test_versions_do_not_duplicate_capability_or_source_run_and_reuse_requires_frozen_snapshot():
    capabilities = [
        {"id": "cap", "name": "old", "status": "draft", "revision": 1, "source_run_id": "source"},
        {"id": "cap", "name": "current", "status": "ready", "revision": 2, "source_run_id": "source"},
        {"id": "second", "name": "second", "status": "draft", "revision": 1, "source_run_id": "source"},
    ]
    direct_reuse = run("reuse-direct", "running")
    direct_reuse["capability"] = {"id": "cap", "revision": 2, "source_run_id": "source"}
    configured_reuse = run("reuse-configured", "queued")
    configured_reuse["capabilities"] = [{"id": "second", "revision": 1, "source_run_id": "source"}]
    unfrozen = run("not-reused", "queued")

    summary = engineering_overview([run("source", "published"), direct_reuse, configured_reuse, unfrozen], capabilities, {"project": "工程"})
    reuse = by_id(summary)["reuse"]

    assert summary["distilled_runs"] == 1
    assert summary["reused_runs"] == 2
    assert summary["draft_capabilities"] == 1
    assert summary["ready_capabilities"] == 1
    assert reuse["count"] == 2
    assert {item["id"] for item in reuse["items"]} == {"cap", "second"}
    assert next(item for item in reuse["items"] if item["id"] == "cap")["title"] == "current"
