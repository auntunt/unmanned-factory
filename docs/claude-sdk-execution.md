# Claude execution in webuddy

The live web application uses `Service -> SDKRunner -> sdk_worker -> control.providers._run_claude`. The legacy `harness/claude_code.py` CLI adapter and its optional permission broker are separate; their defaults do not describe a web run.

Claude receives its standard coding system prompt with the exact workspace, bounded file tools and, on Linux with working bubblewrap, `mcp__project__run_command`. The command tool runs dependency installation, tests and builds without exposing the control-plane environment. Native unrestricted Bash stays unavailable. A terminal availability event is recorded on every execution call.

The command sandbox mounts the root read-only, hides host home, temporary files and runtime sockets, exposes the task workspace for writes and creates a private temporary directory. Git metadata remains read-only because commits/integration belong to the coordinator. Network access remains available for dependencies; this is filesystem/process isolation, not network isolation. Commands do not inherit provider tokens or control-plane credentials. No sudo or root access is required.

Normal absolute paths within the workspace work for Glob, while outside paths and traversal remain denied. Read-only planning does not receive the command tool. If isolation is unavailable, no unisolated command fallback is permitted.

Validation includes real Linux filesystem and environment boundary checks, dependency tool execution, a Python test and timeout termination. Model integration must also demonstrate a real call to the project terminal, not just a mocked SDK response.
