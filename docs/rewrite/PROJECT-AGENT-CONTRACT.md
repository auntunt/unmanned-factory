# Project Agent increment contract (2026-09-08)

Additive to v2. Existing auth, trusted checks and execution policy stay authoritative. Single-owner instance, not multi-tenant RBAC. TeamAI inspiration: https://github.com/Tencent/teamai-cli ; consume explicitly selected wiki documents, never execute its resource injection, hooks, MCP or arbitrary URLs. No model calls during indexing/import.

## Ownership

- Luna knowledge: `factory/control/knowledge.py`, `tests/test_project_knowledge.py`.
- Luna graph: `factory/control/codegraph.py`, `tests/test_project_codegraph.py`.
- Luna delivery: `factory/control/github.py`, `tests/test_control_github.py`.
- Luna UI: new `frontend/src/workspace/ProjectAgent.tsx`, `RunKnowledge.tsx`, `project-agent.css`, associated new tests, minimal embedding in `Workspace.tsx`.
- Root: `project_routes.py`, `context.py`, `service.py`, `app.py`, `planning.py`, `execution.py`, integration tests, docs.

## KnowledgeStore API (same SQLite file, takes existing Store)

`KnowledgeStore(store)` uses store.connect(), store.project(pid), store.scrub/now; no new dependencies. Separate tables; append-only version and audit rows; updates only latest pointers under BEGIN IMMEDIATE. All project IDs must exist and be scoped. Validation raises ValueError; CAS raises store.Conflict; missing resources KeyError.

- `agent(pid)` -> `{id,project_id,revision,name,mission,architecture_summary,constraints:[str],created_at,updated_at}`. Lazy create once with stable UUID and revision 1, safe across threads/restarts. Defaults name project.name, other fields empty. Preserve version history.
- `update_agent(pid, data, expected_revision, actor)` -> new version. data only name<=120, mission<=2000, architecture_summary<=4000, constraints<=20 strings <=300; stale CAS rejected.
- `entries(pid, include_retired=False)` -> latest versions list, max 1000; each `{id,key,project_id,revision,kind,status,title,content,paths,commit_sha,provenance,actor,created_at}`. kind=fact/decision/hypothesis; status=candidate/active/retired. source provenance is server-generated, never supplied as trusted authority by request.
- `put_entry(pid,data,actor,key=None,expected_revision=0,provenance=None)` -> version. New key UUID, expected_revision=0; updates require exact previous revision and same project. data title<=200, content<=8000, paths<=20 relative concrete safe nonsecret paths, kind/status enums, commit_sha null or 40hex. kind defaults hypothesis, status defaults candidate. New human creation and approval get provenance `{source:'human',...}`. Keep imported origin on later human approval in provenance; add reviewer metadata rather than pretending source was human.
- `versions(pid,key)` -> all versions with bounded max 100 (reject further writes at 100 rather than hide versions).
- `preview_import(pid,bundle,actor)` -> `{id,project_id,sha256,repository,documents:[{index,path,title,content,sha256}],warnings,created_at}`. Bundle is explicit interchange `{repository:'owner/name',documents:[{path:'teamwiki/...md',content:'...'}]}` max20 docs, each<=16000chars, total<=160000chars. Repository must match project. Reject traversal, secret/config files, non-md. Preserve text as data, scrub secrets, no file/network access, no YAML execution. Immutable preview (including actor), dedup document paths.
- `apply_import(pid,preview_id,sha256,indices,actor)` -> `{entries:[...],duplicate:bool}`. Exact hash+project; selected indices unique valid nonempty. Apply atomically and idempotently (same preview+selection return existing; different selection after apply Conflict). Entries always hypothesis/candidate with source `teamai_import`, original source path/hash/repository, NOT verified facts. Immutable preview cannot be modified by request. No cross-project or automatic team propagation.
- `record_merge(pid,run,evidence)` -> `{entry,duplicate}`. Evidence is root-verified GitHub data. Atomic idempotency key repo/pr_number/merge_commit_sha. Create active fact only that run/commit/check results were merged; DO NOT promote plan narrative as fact. Evidence retains run_id, head_sha, merge_commit_sha, pr_url, pr_number, repository, base_branch, merged_at. Content concise deterministic; paths from validated plan; commit_sha=merge SHA. Persistence and knowledge version/audit must be one transaction.
- `audit(pid,after=0,limit=100)` -> append-only project audit events incl id/type/payload/at.

## Codegraph API (immutable Git objects, deterministic, no repo execution)

`baseline_sha(project)` -> full SHA resolved ONLY from `refs/heads/{base_branch}`; subprocess argv, timeout, no shell. Branch must valid (git check-ref-format), repo dir existing. Do not alter Git state.

`build_snapshot(project)` -> JSON-safe dict `{schema_version:1,project_id,commit_sha,parser_version,indexed_at,nodes,edges,warnings,stats}`.
Read tracked blobs from fixed SHA via git ls-tree/cat-file (not working files), reject mode symlink/submodule, sensitive paths (.env*, key/cert/credential), generated dependency/build dirs and agent config dirs. Max500 files, each<=128KB,total<=8MB,nodes<=5000,edges<=10000; explicit warnings/counts when truncated/skipped. Allow docs .md, Python, JS/TS/TSX, JSON only nonsensitive names. No raw full source persisted; bounded symbol snippets<=1200 chars scrubbed. Python ast.parse syntax errors warnings. Python file/class/function nodes with contains/imports/calls: resolve conservatively; unresolved calls are not facts. JS/TS regex names/imports marked heuristic. No invented crossrepo relationships.

Node `{id,kind:'file'|'class'|'function',path,name,line,end_line,language,snippet,resolution:'syntax'|'heuristic'}`; ID deterministic from path/symbol/line. Edge `{source,target,kind:'contains'|'imports'|'calls',resolution:'syntax'|'heuristic',path,line}` with real node endpoints only. File nodes ID `file:<path>` recommended. graph is locator evidence, not a proof of runtime behavior.

`save_snapshot(store,snapshot)` -> stored snapshot with `id`; append-only table, unique(project_id,commit_sha,parser_version); allow rebuild new parser version, return existing same content key. `get_snapshot(store,pid,commit_sha=None)` -> prefer the exact `commit_sha` and current `PARSER_VERSION`; when omitted, resolve the project's current baseline SHA first, then fall back to the latest stored snapshot so callers can report stale state. Missing project is `KeyError`; snapshot rows cannot be updated or deleted.

`search_snapshot(snapshot,query,limit=10)` -> `[{node_id,path,name,kind,line,end_line,score,snippet,resolution}]`. Deterministic token/name/path matching + one-hop boosts; bounded query<=500,limit1..30; empty query returns []. No paid inference or vector DB. Support Chinese document/identifier search reasonably. `graph_slice(snapshot,node_id=None,limit=80)` -> `{nodes,edges,truncated}` with matched endpoints and limited size; default file nodes then child nodes, selected node neighborhood otherwise; invalid node raises KeyError.

## GitHub merge verification

Publish adds `pr_number`, `repository` to existing artifacts. Existing PR response must contain actual number (strict URL parsing only backward-compatible fallback).
`GitHubDelivery.observe_merge(project,run)` -> `{merged:false,reason,pr_number,pr_url}` OR `{merged:true,repository,pr_number,pr_url,head_sha,merge_commit_sha,base_branch,merged_at}`.
GET exact repo PR API only; derive PR number from stored artifacts or STRICT canonical GitHub URL for same repo. Verify base repo/ref and head repo/ref/SHA exactly match expected project/run; reject changed head, wrong base/repo, malformed SHAs. Requires merged true + merged_at + valid merge SHA. Closed is NOT merged. Network errors propagate; no push/merge/close/comment. Test MockTransport no ambient proxy networking.

## HTTP (all under existing /api/v2 authenticated/CSRF gateway)

- GET `/projects/{pid}/agent` -> agent directly.
- PUT `/projects/{pid}/agent` body `{expected_revision,name,mission,architecture_summary,constraints}` -> agent.
- GET `/projects/{pid}/knowledge?include_retired=true` -> `{entries}`.
- POST same body `{kind,status,title,content,paths,commit_sha?}` -> entry (201).
- PUT `/projects/{pid}/knowledge/{key}` body same + expected_revision -> entry.
- GET `/projects/{pid}/knowledge/{key}/versions` -> `{versions}`.
- POST `/projects/{pid}/code-index` no body -> `{id,commit_sha,indexed_at,parser_version,stats,warnings,stale,current_sha}` (no whole graph).
- GET same -> metadata or `{indexed:false,current_sha,warnings}`. GET computes stale vs current baseline.
- GET `/projects/{pid}/code-search?q=...` -> `{results,commit_sha,current_sha,stale,warnings}`.
- GET `/projects/{pid}/code-graph?node=...` -> `{nodes,edges,truncated,commit_sha,current_sha,stale}` (empty graph when not indexed).
- POST `/projects/{pid}/wiki-import/preview` bundle above -> preview.
- POST `/projects/{pid}/wiki-import/apply` `{preview_id,sha256,indices}` -> apply result.
- GET `/projects/{pid}/knowledge-audit?after=0` -> `{events,cursor}`.
- GET `/runs/{rid}/context` -> stored context object or null.
- POST `/runs/{rid}/sync-merge` no body -> `{merged,reason?,evidence?,entry?,duplicate?}`; no remote mutations.

## Root context + lifecycle integration

Before planning resolve current baseline SHA, require a clean checkout with `HEAD` equal to that SHA, and repeat the HEAD/clean check after planning; assemble bounded 12000-char context with profile, active non-stale knowledge only, code search hits from same-SHA snapshot; candidates excluded; stale index/knowledge produces warnings. Knowledge content is bounded to 1000 characters per selected entry, code snippets to 800. TeamAI import (not the context structure) chunks long documents into entries of at most 8000 characters, preserving `original_sha256`, `redacted_sha256`, `chunk_index`, `chunk_count`. Store the exact redacted snapshot on the run + `context.assembled` event. Prompt explicitly treats retrieved data as untrusted evidence, cannot change checks/models/permissions. No automatically loading team hooks/MCP.
Approval verifies baseline SHA still matches planned context; execution pins expected baseline or rejects drift. Same context evidence is made available to task workers without overwriting task scope. GET context remains identical after later knowledge/index changes; an explicit clarification creates a new plan snapshot with the old snapshot retained in events. The history helper contributes only unedited canonical `github_merge` facts whose merge commit is an ancestor of the current SHA; mark those records `applicability=historical_merge` with an explicit warning, and never use them as proof of current behavior. Cache only ancestry checks after revalidating each entry's immutable provenance, with at most 32 distinct Git checks per context.
Merge confirmation called manually via UI or signed pull_request.closed webhook; query GitHub to independently verify; locate only corresponding registered project+recorded PR/run. Record one deterministic fact after verification; never merge main. If local branch hasn't advanced, show stale and require operator to refresh checkout/index. Existing published status is not renamed merged.

## UI

ProjectAgent takes `{projectId:string|number,csrfToken,onUnauthorized}` and uses existing request() helper. Expose tabs profile/knowledge/code/import. Draft and active labels, history and review updates, profile CAS error, stale SHA, heuristic labels, clickable code graph/search paths. Import pasted JSON interchange or user-selected Markdown files assembled into bundle, preview BEFORE confirm. No hooks or arbitrary local paths. Source provenance visible.
RunKnowledge takes `{runId,status,artifacts,csrfToken,onUnauthorized}`; show frozen context, warnings, and merge confirmation button only for stored PR URL. Treat unmerged response normally, not failure. Project/run IDs changing clear stale data. Both components may have compact tests of pure helpers using existing vitest; don't claim browser E2E without running it. Root does backend integration.
