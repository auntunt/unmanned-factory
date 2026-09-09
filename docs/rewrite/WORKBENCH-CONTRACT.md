# Workbench and runtime contract (2026-09-08)

This increment replaces the main UI and completes a v2 installation path. It reuses existing authenticated APIs, project knowledge and execution controls. Server deployment context: project `/home/ubuntu/workspace/unmannedfactory`, existing service `factoryweb.service`, Caddy origin `https://harness.cloudwaveai.cn`. Root alone connects/deploys. Preserve actual server settings; never enable the old factoryapi service. Subagents do not read credentials.

## Ownership

- Luna runtime: `factory/control/runtime.py`, `runtime_routes.py`, `tests/test_runtime.py`, `tests/test_runtime_routes.py`.
- Luna deployment: `factory/control/runtime_cli.py`, `deploy/install-control.sh`, `deploy/factory-control.service`, `deploy/control.env.example`, `deploy/HERMES-HANDOFF.md`, `deploy/README.md`, `tests/test_runtime_cli.py`. Do not edit app.py/pyproject; give root exact entrypoint patch.
- Luna shell: `frontend/src/App.tsx`, `frontend/src/workbench/Workbench.tsx`, `OverviewPage.tsx`, `ProjectsPage.tsx`, `ProjectPage.tsx`, `ui.tsx`, `workbench.css`, shell tests. Own login and router; other page imports use lazy imports.
- Luna detail: `frontend/src/workbench/RunPage.tsx`, `RuntimePage.tsx`, `runtime-types.ts`, `detail.css`, associated tests. Export default components. Do not edit shell/styles owned by shell agent.
- Root: app/service/store/execution/planning integration, project editing, final tests/docs/GitHub/server access.

All agents use `.venv/bin/pytest` instead of plain `uv run`, which may resync without SDK extras. No paid calls or server changes by subagents.

## UI shared contract

Base theme: 220px graphite sidebar, warm white #F5F5F2 canvas, white content, text #18201E, teal #0F766E, border #DDE3DF, 8/12px radii. Chinese product copy, responsive 390px mobile navigation, no fake records or fake online states. Pages: `/`, `/projects`, `/projects/:projectId`, `/runs/:runId`, `/settings/runtime`.

All pages receive `{csrfToken:string,onUnauthorized:()=>void}`. IDs from react-router `useParams`. Shell owns BrowserRouter/Routes/auth; pages can use Link/useNavigate. Shared `ui.tsx` exports `PageHeader({title,description?,actions?})`, `EmptyState({title,description?,action?})`, `StatusBadge({status})`, `ErrorNotice({message})`, `formatDate(value)`, `statusLabel(status)`. Plain ReactNode children and existing request helper `../workspace/api`. Details agent may use its own primitives until ui exists; no new package deps.

Overview loads real projects/runs/runtime independently. Triage in user copy is '需要确认'. Routes are lazy loaded; discard main UI's legacy admin/ControlRoom imports and giant bundle. Existing legacy files may remain unreferenced for compatibility but are not navigation. Sidebar: 工作台、项目、运行配置. Main actions start requirement, answer clarification, approve current revision, view delivery. Model IDs never invented; Astra/Luna are this development team's chosen models, not automatically valid runtime account IDs.

Project page can reuse ProjectAgent with new compatible theme scoped wrappers; must expose real knowledge/code/import abilities. Settings edit project name/checks/base branch/automation/budget via new PUT below; workspace/repository are immutable. Requirements remain existing NewRun POST. Advanced technical details belong in collapsible sections; primary flow no raw JSON wall.

## Runtime config API and module contract

`RuntimeSettings(store, profiles=None, limits=None)` SQLite persistence with immutable versions/audit and CAS. `get()` -> `{revision,profiles,limits,updated_at}`. Defaults from provided profiles (else FACTORY_* environment) and limits `{timeout_s:600,max_parallel:2,max_tasks:20,unknown_cost_policy:'allow_bounded'}`. `update(data,expected_revision,actor)` validates complete four-role mapping (`planner,cheap,standard,strong`), provider in claude/codex/dsh, model string <=160 (empty means incomplete config), timeout30..1800, parallel1..4, max_tasks1..20, policy stop/allow_bounded. Reject DSH as planner. No credentials/URLs/paths/commands in writable schema. ValueError->400, Conflict->409. Get preserves existing config across process restart; environment only seeds initial config.

`inspect_runtime(settings, workspace_root, static_dir)` -> `{checked_at,execution_mode:'local_sdk_children',configuration_revision,profiles,limits,tools:[{id,installed,version,import_status,capabilities:{read_only,workspace_write},auth:'present'|'missing'|'unknown',issues:[{code,message,remediation_id}]}],host:{python_version,git_available,node_available,hermes_available,frontend_built},readiness:{planning:boolean,execution:boolean,publishing:boolean},blockers:[str],last_probes:[...]}`. No secret values or auth file contents returned. Import/binary checks bounded subprocess same sys.executable; cache short <=10s; never a paid call on GET; auth hints != live verification. `local_readiness` reports deterministic local checks; `readiness` and `live_verified` require an explicitly recorded successful probe at the current configuration revision. They do not guarantee future availability. Avoid requiring standalone PATH CLI when SDK includes runtime. Errors explicit and scrubbed. Host checks exact known executables, no discovery scan/config dump.

RuntimeRoutes `router(store,service,workspace_root,static_dir)` uses service.runtime_settings instance; mount behind existing auth/CSRF. GET `/api/v2/runtime`; PUT `/api/v2/runtime/profiles` body `{revision,profiles,limits}` -> config; POST `/api/v2/runtime/probe` `{profile,configuration_revision}` -> `{id,profile,provider,model,configuration_revision,checked_at,outcome:'passed'|'failed'|'unsupported',message}` persisted+audit. Probe fixed short read-only prompt in TemporaryDirectory outside repositories, timeout<=30sec, max one probe in process; no arbitrary prompt/path/command accepted, DSH unsupported. Explicit click authorizes potential billable call; no automatic probes. Stale revision409; transient provider error sanitized; report real outcome, never infer readiness from test alone. GET runtime returns last probes relevant to current revision.

Runtime module exposes `profile_blockers(profile,role)` -> list[str] for obvious missing model/SDK/capability; it must not pretend absent env means OAuth cannot work. Root calls only for real SDKRunner; injected test runners remain explicit test dependencies.

## Root lifecycle and project settings

At planning freeze current config on run as `runtime_configuration`; planner and all task workers use that snapshot. Editing settings affects only newly planned runs. Keep svc.profiles initialized for compatibility but old /providers returns current runtime_settings.get profiles. Freeze task-count/timeout/concurrency and enforce actual run task count. Existing runs without snapshot use compatibility defaults.

Unknown costs remain null. Default stop preserves existing behavior. Explicit `allow_bounded` allows later waves only within max_tasks/timeout/concurrency limits; always persist billing_incomplete and block automatic publication when any cost unknown. Budget in dollars cannot be promised when billing is unknown; UI explains this tradeoff before saving.

Project objects add revision (legacy default1). PUT `/api/v2/projects/{pid}` `{revision,name,base_branch,checks,auto_issues,auto_publish,budget_usd}` ->project. Reuse create validation; repository/workspace immutable; reject edits while any active/waiting/ready-for-review run exists, published/cancelled runs okay. Audit setting changes. GET `/api/v2/projects/{pid}/readiness` -> `{project_id,ready,checks:[{id,label,status:'ok'|'blocked'|'warning',message}],checked_at}` with cleanHEAD/base/repo/executablecheck definitions checks; don't execute project checks. Root owns endpoints.

## Installation

Provide reviewed idempotent script for an existing checkout: uv sync --frozen --all-extras --no-dev (matching service interpreter), npm ci/build, explicit checks and doctor. No API keys written/printed, no default passwords, no autoenable services or firewall changes. Operator passes exact checkout/user/environment. New systemd service uses `.venv/bin/factory-web serve`, protected external env file; avoid `uv run` resync dropping extras. Hermes handoff gives concrete inspection/install/config steps and secret handling; user can run `hermes chat --query-file <handoff>` only after confirming installed CLI supports it. Never treat Hermes as a fourth worker SDK.

CLI `factory-runtime doctor [--json]` uses same runtime diagnostics without paid calls, `factory-runtime config ...` only if bounded well-defined options; can keep role config via authenticatedUI. Probe CLI optional, same exact validation and boundaries if added. Root adds entrypoint to pyproject. Report package installation and live authentication separately.

Unknown-cost delivery behavior: new runtime settings default to `allow_bounded`; existing explicit `stop` settings remain persistent. Missing provider prices remain unknown and do not prevent verified artifact collection or download. Automatic publication remains blocked for incomplete billing. To resume an older cost-paused run, set the current runtime policy to `allow_bounded` and explicitly continue the run. Continuation changes only the frozen unknown-cost policy, preserving models, execution limits, plan and saved task evidence. Null or invalid delivery commits return an empty catalog and 404 for unavailable downloads, never a type error.
