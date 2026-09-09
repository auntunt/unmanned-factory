# Project helpers and learning disposition

A project may select a primary職能体. New runs freeze that agent's active version and resolved planning, execution and verification models. Changing the project selection or applying a new agent version does not rewrite existing run snapshots. Existing repository connection remains available as a secondary workspace source.

The project knowledge page combines the selected agent's instructions, acceptance criteria and applied Skill assets with project knowledge and delivery capability candidates. These sources remain scoped to their original project and retain their revisions and source-run links.

Each learning has an explicit disposition:

- Standalone: retain an immutable source snapshot and export it as a ZIP containing `SKILL.md` and `source.json`. The ZIP can be uploaded through the existing agent maintenance Skill importer.
- Agent: append the source material to the selected agent's draft, preserving existing instructions, acceptance criteria and unresolved conflicts. Draft update and disposition insertion share one SQLite transaction. Applying the draft produces the normal immutable agent version. Project records show whether the current active version contains the source marker.

A source cannot silently change destinations after disposition. Repeated identical submissions return the existing record; stale source or draft revisions return a conflict. New information can be retained as a new entry.

Authenticated APIs:

- `GET/PUT /api/v4/projects/{pid}/assistant` reads/changes the primary helper with `expected_revision`.
- `GET /api/v4/projects/{pid}/learnings` returns scoped source entries and disposition metadata.
- `POST /api/v4/projects/{pid}/learnings/settle` accepts `source_id`, `source_revision`, `destination`, optional `agent_id` and `draft_revision`.
- `GET /api/v4/projects/{pid}/learnings/export?source_id=...` exports a standalone source snapshot.
- Workspace creation and repository connection accept an optional `agent_id`.

Read-only maintenance retries provider overload responses (429/503/529 or explicit overload text) at most twice within the original total deadline, with cancellation-aware backoff. Authentication and validation errors are not retried. Failed maintenance keeps conversation and uploaded material; `POST /api/v4/conversations/{cid}/retry` restarts failed/cancelled/interrupted maintenance without duplicating the user message. Existing access and CSRF checks continue to apply.

Validation covers binding and frozen versions, stale revisions, atomic/idempotent consolidation, conflict preservation, standalone ZIP reimport, overload retry/cancellation, retained retry inputs, and browser walkthroughs of workspace selection and draft application. Browser walkthroughs use an isolated scripted provider, not a live model service.
