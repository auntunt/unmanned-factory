# Dependency-driven execution

webuddy dispatches up to the existing `max_parallel` limit (default two). It waits for the first completed task, integrates verified commits serially, saves a checkpoint, and immediately releases newly eligible dependents. Overlapping declared paths are serialized. Failed dependencies block their descendants, while independent branches continue; final delivery still requires the complete plan and final checks to pass.

On the two-core host, project terminal commands and trusted checks share one process-wide/user-wide file-lock slot. Model requests can overlap while commands wait. Lock waiting counts toward the command/check deadline and releases on process exit. This is admission control, not a hard CPU/memory quota: individual tools may still create multiple threads. Provider-native tools outside the project terminal are not covered by this command slot.

Task activity distinguishes model work, project commands, trusted checks, and capacity waiting. Checkpoints synchronize task states in the API, and the execution page displays dependency waits. Attempt durations and command wait durations remain recorded. Existing active calls adopt the new scheduler only after a saved continuation; plans are not rewritten automatically.

Validation covers a dependent starting while an unrelated slow task is still running, an independent branch completing after another fails, bounded/cancellable command-slot waiting, draft recovery, deadlines and UI status rendering.
