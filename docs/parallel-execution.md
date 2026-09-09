# Dependency-driven execution

webuddy dispatches up to the existing `max_parallel` limit (default two). It waits for the first completed task, integrates verified commits serially, saves a checkpoint, and immediately releases newly eligible dependents. Overlapping declared paths are serialized. Failed dependencies block their descendants, while independent branches continue; final delivery still requires the complete plan and final checks to pass.

On the two-core host, project terminal commands and trusted checks share one process-wide/user-wide file-lock slot. Model requests can overlap while commands wait. Lock waiting counts toward the command/check deadline and releases on process exit. This is admission control, not a hard CPU/memory quota: individual tools may still create multiple threads. Provider-native tools outside the project terminal are not covered by this command slot.

Task activity distinguishes model work, project commands, trusted checks, and capacity waiting. Checkpoints synchronize task states in the API, and the execution page displays dependency waits. Attempt durations and command wait durations remain recorded. Existing active calls adopt the new scheduler only after a saved continuation; plans are not rewritten automatically.

Validation covers a dependent starting while an unrelated slow task is still running, an independent branch completing after another fails, bounded/cancellable command-slot waiting, draft recovery, deadlines and UI status rendering.

## Repair continuity and check failures

Before a model is dispatched, each configured check executable must be available in the actual check environment. This is a launch probe, not a requirement that unfinished features already pass tests. Configuration failures (missing executable, exit 126/127, invalid arguments) do not trigger model escalation. Import/dependency failures permit at most one same-model repair before pausing; they are not proof that application code is correct or that the check itself is broken.

Retries retain the working tree and dependency environment. Same-provider/model retries pass the returned SDK session ID; a model change starts a new conversation with the existing files and check evidence. Changed declared files are copied to an attempt snapshot before repair, and diagnostic output remains in the attempt record. The first repair uses the same model; later functional failures may escalate within the existing attempt cap. This does not yet reconnect a provider session across a human continuation into a different worktree.

## Supporting changes within a task

A file list describes primary task ownership, but a resource-producing task may also require a packaging declaration. The executor now accepts a narrowly verified additive change to `tool.setuptools.package-data` even when `pyproject.toml` was omitted from that task's plan. All other parsed TOML content must remain identical; existing entries cannot be removed, and newly included files must be declared task resources. Wildcard package names, traversal, symlinks, executable resource extensions and unrelated newly included files are rejected. Scope exceptions produce a `scope.supporting_change` audit event and still pass normal protection and verification checks.

Resource tasks reserve the shared packaging manifest during scheduling to avoid concurrent implicit writes. Planning instructions include supporting packaging files explicitly for future plans. Other out-of-scope changes remain `scope_violation`, distinct from timeout; this policy does not authorize arbitrary dependency, test, CI or deployment changes.

The paused splitter's `.md` inclusion is an example: the prompt file belongs to its declared task, and the sole supporting change includes that file in built packages. Continuation retains the original plan and draft and revalidates it under this rule.

## Generated output versus delivery inputs

Untracked standard `*.egg-info` metadata is excluded from scope comparison, even when a project's ignore file is incomplete. Ignored egg-info directories are inspected for unexpected files; source files and symlinks are not exempt. Tracked changes always remain visible, including tracked caches and metadata. Files are never stashed or deleted to make checks pass.

`uv.lock` remains a delivery input: dependency tasks must explicitly declare it, and ordinary scope checks apply. A build-artifact exemption does not authorize `config.py`, dependency changes or unrelated source edits. Adding an ignore rule alone never authorizes a source change.
