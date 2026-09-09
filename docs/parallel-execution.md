# Dependency-driven execution

webuddy dispatches up to the existing `max_parallel` limit (default two). It waits for the first completed task, integrates verified commits serially, saves a checkpoint, and immediately releases newly eligible dependents. Overlapping declared paths are serialized. Failed dependencies block their descendants, while independent branches continue; final delivery still requires the complete plan and final checks to pass.

On the two-core host, project terminal commands and trusted checks share one process-wide/user-wide file-lock slot. Model requests can overlap while commands wait. Lock waiting counts toward the command/check deadline and releases on process exit. This is admission control, not a hard CPU/memory quota: individual tools may still create multiple threads. Provider-native tools outside the project terminal are not covered by this command slot.

Task activity distinguishes model work, project commands, trusted checks, and capacity waiting. Checkpoints synchronize task states in the API, and the execution page displays dependency waits. Attempt durations and command wait durations remain recorded. Existing active calls adopt the new scheduler only after a saved continuation; plans are not rewritten automatically.

Validation covers a dependent starting while an unrelated slow task is still running, an independent branch completing after another fails, bounded/cancellable command-slot waiting, draft recovery, deadlines and UI status rendering.

## Repair continuity and check failures

Before a model is dispatched, each configured check executable must be available in the actual check environment. This is a launch probe, not a requirement that unfinished features already pass tests. Configuration failures (missing executable, exit 126/127, invalid arguments) do not trigger model escalation. Import/dependency failures permit at most one same-model repair before pausing; they are not proof that application code is correct or that the check itself is broken.

Retries retain the working tree and dependency environment. Same-provider/model retries pass the returned SDK session ID; a model change starts a new conversation with the existing files and check evidence. Changed declared files are copied to an attempt snapshot before repair, and diagnostic output remains in the attempt record. The first repair uses the same model; later functional failures may escalate within the existing attempt cap. This does not yet reconnect a provider session across a human continuation into a different worktree.
