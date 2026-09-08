# v3 parallel implementation contract

All work uses `/Users/auntlee/Desktop/自动化harness构建`. Preserve v2 endpoints and existing tests. New routes precede the catch-all legacy API. Authentication/CSRF remains at app middleware. No fabricated production data, costs, model availability, or live provider success.

## Ownership

- Root: `service.py`, `store.py`, `app.py`, new `autonomy.py`/`autonomy_routes.py`, durable scheduling/recovery, archive exports, integration/docs/browser acceptance.
- Routing worker: `execution.py`, new `model_routing.py`, routing tests. Do not edit runtime.py, service.py, store.py or app.py.
- Capability worker: new `capabilities.py`, `capability_routes.py`, capability tests. Do not edit app/service/store/runtime or frontend.
- Frontend worker: frontend App and workbench/workspace types, pages and CSS; may add tests. No backend edits.

## Autonomy API (root)

`GET /api/v3/projects/{pid}/policy` -> `{revision, mode, max_risk, max_attempts, auto_escalate, resume_on_restart}`

`PUT /api/v3/projects/{pid}/policy` takes the same object with expected revision. mode: supervised|autonomous; max_risk: low|medium|high; max_attempts: 1..3; auto_escalate/resume_on_restart booleans. Initial mode is supervised for migrated projects; frontend may explicitly enable autonomous on new projects. No repeated confirmation for ordinary authorized work.

`POST /api/v3/runs/{rid}/retry` -> new linked Run with frozen request plus failure summary and source.previous_run_id. Manual explicit action retries planning/execution using new isolated workspace, never replays publish blindly.

`GET /api/v3/overview` -> `{projects: number, runs: number, active_runs: number, attention_runs: number, delivered_runs: number, known_cost_usd: number, unknown_cost_runs: number, recent_events: [{...Event, project_name?, run_title?}], model_usage: [{profile, model, calls, known_cost_usd, unknown_cost_calls}], activity: [{date,runs,delivered}], capabilities: number}`. Values aggregate recorded store data; no invented savings.

`GET /api/v3/runs/{rid}/export?format=json|markdown|zip` -> redacted attachment containing complete recorded run events, plan/config/context and artifacts. JSON default.

Existing v2 run/project/runtime APIs continue. Run gains optional `policy`, `capability`, `previous_run_id`, `planner_usage`. Project `autonomy_mode` is not added to existing strict v2 update schema; use separate policy endpoint.

## Routing/execution contract

Root passes `project['routing_policy'] = {max_attempts, auto_escalate}` from frozen policy and subtracts known planner cost from project budget. Existing callers without routing_policy retain single attempt behavior.

Add `model_routing.select_profile(task, profiles, attempt=1, auto_escalate=True)` returning `{profile, provider, model, reason, attempt}`. Follows existing complexity/risk classification and prefers configured compatible roles; absent configuration is a visible error, not a pretend successful run.

Execution emits `model.selected` payload `{profile, provider, model, reason, attempt}`, `attempt.started`, `attempt.completed`/`attempt.failed`, and `usage.recorded` payload `{profile,provider,model,attempt,cost_usd,input_tokens?,output_tokens?,cached_input_tokens?}`. Every charged attempt is recorded even if subsequent checks fail. Task result retains `attempts` list. Do not weaken workspace and trusted-check guards.

Retry only repairable execution/verification failure, same bounded scope, with failure evidence appended to prompt; no retry on cancellation, exhausted deadline, policy/metadata violations, or event sink failure. Total known/unknown cost spans all attempts, not just successful one. Task-level worker emits terminal task.failed only when exhausted, not on intermediate retry. Artifacts include known_cost_usd/observed_cost_usd/billing_incomplete and attempts evidence.

## Capability API (capability worker)

`GET /api/v3/capabilities` -> `{capabilities: Capability[]}`

`POST /api/v3/capabilities` -> Capability. Body `{name,description,category,instructions,input_description,output_description,acceptance:string[],status?:draft|ready}`. category: engineering|operations|collaboration|integration|migration|custom. Requirements must be nonempty to mark ready. Each capability has `{id,revision,...body,created_at,updated_at,source_run_id?:string}`.

`GET /api/v3/capabilities/{id}` -> Capability plus `versions: Capability[]`.

`PUT /api/v3/capabilities/{id}` -> Capability. Body same plus `expected_revision`; preserve history. Ready means user-configured invocation contract, not claimed live certification.

`GET /api/v3/capabilities/{id}/export?revision=N&format=zip|json` exports an exact version. ZIP contains agent.json, SKILL.md, README.md and source-run.json when sourced from a delivery. This is a portable definition, not a bundled model runtime or cloud environment.

`GET /api/v3/projects/{pid}/capabilities` -> `{bindings: [{capability_id,revision,name,status}]}`

`POST /api/v3/projects/{pid}/capabilities` body `{capability_id,revision}` -> binding; freezes exact revision. `DELETE /api/v3/projects/{pid}/capabilities/{id}` removes binding.

`POST /api/v3/capabilities/{id}/invoke` body `{project_id,revision,request}` -> Run, calls store.create_run and service.start_plan with source `{type:'capability', capability_id, capability_revision}` and stores `capability` frozen snapshot on run before start_plan. Root will inject snapshot into planning/execution context. Reject draft/unready versions and revision mismatch.

`POST /api/v3/runs/{rid}/distill` body `{name,description?,category?}` -> draft Capability with source_run_id, instructions based on recorded request/plan/evidence. Only verified/published runs; deterministic candidate extraction is explicitly labelled, not represented as learned/validated global truth. Service also captures one draft automatically after successful verification, with run.capability_candidate_id and capability.candidate_created event; it does not auto-promote unvalidated cross-project knowledge.

Module router signature `router(store, service)`. Seed 5 clearly draft templates (project maintenance, cloud deployment, Feishu progress reporting, external API integration, legacy migration), no live CLI calls or credentials; these are editable capability contracts that become executable via existing coding agents once configured and marked ready. `CapabilityStore(store)` exposes `list()`, `get(id, revision=None)`, `bindings(project_id)` for root context/overview.

## Frontend

Chinese, restrained high-quality operational workbench. Reorganize navigation: 总览 / 运行看板 / 项目 / Agent 能力库 / 模型与成本. Runtime settings remain available. Light neutral base, graphite text, green/teal accent, consistent typography/tabular numbers, clear async feedback and mobile adaptation. Prefer live activity + task board + explicit empty-state onboarding over decorative dashboard stats.

Run details add readable model-routing/attempt evidence, retry, multi-format export, distill candidate. Project settings include policy and capability bindings. Capability list/editor/invoke actually calls endpoints. Cost page reports known costs and unknown costs distinctly; no hardcoded model IDs or claimed dollar savings. Preserve old auth/project/runtime functionality. No marketing hero or decorative photos needed for product UI.
