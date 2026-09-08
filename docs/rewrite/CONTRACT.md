# Engineering Harness v2 — implementation contract

The rewrite lives in `factory/control/` alongside the legacy engine. All SDK types stay behind providers.py. The existing API is private behind the new authenticated gateway; the legacy `factory api` command remains loopback-only for local compatibility.

## Product boundary

Single-owner engineering workstation in this milestone. All locally created accounts are trusted engineers with equal workspace access, NOT separate tenants. A web login does not sandbox coding agents. Deploy workers under a dedicated OS user/container without control-plane or GitHub credentials. Branch/PR publication belongs to the orchestrator.

## HTTP contract (new service, same origin)

JSON responses, errors `{detail: string}`. Every endpoint except login and signed GitHub webhook requires session cookie. Every mutation requires `X-CSRF-Token` from login/me; Origin must match configured public origin. No public registration. Body limit 1 MiB.

- POST `/api/auth/login` `{username,password}` -> `{user:{id,username},csrf_token}` and HttpOnly SameSite=Strict session cookie.
- GET `/api/auth/me` -> same body; POST `/api/auth/logout` -> `{ok:true}`.
- GET `/api/v2/projects` -> `{projects:[Project]}`.
- POST `/api/v2/projects` -> Project. Fields `name`, `repository` (owner/name), `workspace` (existing server checkout), `base_branch` default main, `checks` mapping names to argv arrays, `auto_issues` false, `auto_publish` false. Owner config only, not inferred from issue text. Workspace constrained to configured workspace root.
- GET `/api/v2/providers` -> `{providers:[{id,installed,detail}],profiles:{planner:{provider,model},cheap:...,standard:...,strong:...}}`. Provider secrets never returned.
- GET `/api/v2/runs` -> `{runs:[Run]}`.
- POST `/api/v2/runs` `{project_id,request}` -> Run, status `received`; starts background planning.
- GET `/api/v2/runs/{id}` -> Run including plan/tasks/triage/artifacts.
- POST `/api/v2/runs/{id}/clarify` `{answer}` -> Run; appends answer and replans, incrementing plan revision. Prior approval invalid.
- POST `/api/v2/runs/{id}/approve` `{revision}` -> Run; only current revision without unresolved questions can execute. Starts bounded DAG scheduler.
- POST `/api/v2/runs/{id}/cancel` -> Run; cancel worker process groups.
- POST `/api/v2/runs/{id}/publish` -> Run; only verified delivery branch. Git push + PR via configured GitHub token; no auto-merge.
- GET `/api/v2/runs/{id}/events?after=0` -> `{events:[Event],cursor:int}`; bounded cursor polling over persisted records.
- GET `/api/v2/runs/{id}/conversation` -> `{messages:[{id,role,content,event_ids,at}]}`; a deterministic projection, not fabricated dialogue.
- GET `/api/v2/runs/{id}/export` -> Markdown conversation + evidence references.
- POST `/api/v2/github/webhook` -> signed issues event; HMAC-SHA256, delivery id dedup and repository allowlist. Ignore PR events, unsupported actions, non-opted-in projects. Webhook only analyzes; execution needs explicit project auto_issues plus factory-ready label plus low-risk sufficiently specified plan.

## Objects

Run: `id,project_id,request,status,revision,plan,triage,tasks,artifacts,created_at,updated_at`. The Project Agent increment adds frozen `context` and independently verified merge evidence; its [contract](PROJECT-AGENT-CONTRACT.md) extends this initial milestone, including signed `pull_request.closed` intake for observation only.
States: `received,planning,needs_clarification,awaiting_approval,queued,running,verifying,ready_for_review,publishing,published,needs_human,failed,cancelled`.
Artifacts: `branch,base_sha,commit,pr_url,checks,worktree`; missing values absent/null, never fictional.

Plan JSON: `{title,summary,questions:[str],tasks:[{id,title,prompt,acceptance:[str],paths:[str],checks:[str],depends_on:[str],complexity:'small'|'medium'|'large',risk:'low'|'medium'|'high'}]}`.
Task ids safe ASCII, unique; DAG validated and max 20 tasks. paths repository-relative, non-empty, no traversal, no .git or absolute paths. A path may name a file or directory. Checks reference the PROJECT's trusted named commands (never execute commands extracted from Issue text). Every executable task has acceptance/paths/checks. A plan with gaps remains in needs_clarification, not auto-fixed by inventing defaults.
Visualization derives from the SAME plan: task DAG + scope/acceptance cards. Selecting a node shows criteria and dependencies. Human correction appends a clarification tied to current revision and regenerates plan. Plan summary is an observable explanation, not hidden chain-of-thought.

Triage: `{decision:'auto_execute'|'needs_clarification'|'human_approval',reasons:[str],questions:[str],risk:'low'|'medium'|'high'}`. Product ambiguity, missing checks, authentication/payment/migration/deployment paths -> human. No self-reported LLM confidence as authorization. Check definitions editable only as trusted project settings.

Event: `id` monotonic SQLite row id, `version:1,run_id,task_id|null,type,payload,at`. Payload recursively redacted before persistence; no passwords, API keys, private reasoning. Event types include user.message, plan.created, triage.decided, human.approved, task.queued, task.started, assistant.message, tool.call, tool.result, provider.session, provider.usage, check.result, git.commit, github.published, run.failed, run.cancelled. User-facing Q&A only actual user and assistant messages/decision summaries, linked to event IDs.

## SDK contract

`ProviderRequest(provider,model,prompt,workspace,session_id=None,timeout_s=600,read_only=False)`.
`ProviderResult(text,session_id=None,cost_usd=None,tokens_in=None,tokens_out=None)`.
`SDKRunner.run(request,emit,cancel=None)`; `available()` lists installed optional SDKs.
No automatic provider fallback after permission or transport errors. Unknown billing is null, not 0. Provider/model/profile identifiers configured by operator; Luna here is the requested implementation subagent, not a promise every vendor account exposes a Luna model id.

## Scheduling and delivery

One coordinator per run, bounded ThreadPool for independent tasks; each child gets its own worktree. Dependencies run on integrated parent commit. Parent branch receives independently verified child commits serially. Re-run project checks on final integration. Overlapping paths cannot run concurrently. One failed branch blocks downstream work; preserve workspace and evidence. Never remove an existing attempt's worktree to retry.
Use trusted project checks with timeout, record command/output/exit status. Run checks after code capture; reject test tampering/scope escape and changed HEAD. Gate evidence binds to final diff/commit. Do not stage arbitrary unreviewed files or use blanket `git add -A`.
GitHub delivery uses dedicated credentials in control plane only; idempotent branch and PR, explicit base branch and exact verified head. No force push or automatic merge. Store publication attempts and reconcile on retries.
On process restart, unfinished states become needs_human with explanation; never silently replay a task that may already have external effects. SQLite WAL + transactions for event/state updates, signed webhook dedup.

## Milestones

1. Design + tested vertical implementation (login, project, intake/plan, visual confirmation, bounded execution, events/Q&A, Issue intake, PR delivery).
2. Provider live certification per configured account; container boundaries; crash lease/fencing and resumable jobs; true per-project ACL; GitHub App tokens and durable outbox reconciliation.
3. Rich visual mockups/annotations and preview, independent semantic reviewers, cost feedback routing and approved low-risk auto-merge policy.

Never describe stages 2/3 or mocked provider runs as completed production capabilities. Offline tests must cover real orchestration with fake providers and local Git remotes; paid SDK integration requires installed credentials and is reported separately.
