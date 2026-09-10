# Isolated project browser

Platform-owned Puppeteer runtime; projects do not install browser dependencies. Requires Linux, Node 18+, existing Chrome, and the persistent `TerminalSession` bubblewrap sandbox. It never attaches to an existing browser or reads a host profile.

Install `package.json`, `package-lock.json`, and `bridge.mjs` in administrator-owned `/opt/webuddy-browser`, then run there:

```sh
npm ci --omit=dev --ignore-scripts --no-audit --no-fund
```

No browser download occurs. Defaults are `/opt/webuddy-browser` and `/usr/bin/google-chrome`; service variables `FACTORY_BROWSER_RUNTIME` and `FACTORY_BROWSER_CHROME` override these paths. The runtime is read-only to project processes.

`BrowserSession(workspace, terminal_session)` and `create_tools(session, emit)` append five tools to the existing `project` MCP server. Add `TOOL_NAMES` to its allowlist. Close the browser before closing its terminal. Each call holds one platform command slot, including startup. Events include timing and observation metadata, never the submitted fill arguments.

Start project previews bound to `127.0.0.1` (IPv4 `0.0.0.0` also works). `browser_open` accepts an explicit HTTP port and optional width 320–1920 / height 320–1600; width <=600 enables mobile input emulation. Only the selected origin, owned by processes in this terminal's PID namespace, can load. The browser's enforced proxy also blocks external requests, host services, redirects to other origins, and proxy CONNECT. External CDNs must be bundled locally. This is a preview tool, not an internet research browser. A malicious project server can itself proxy requests; project network isolation remains the terminal's responsibility.

Snapshots return fresh element refs, visible text, viewport and errors; use current refs after each action. Responses are limited to 24 KB of UTF-8 JSON and mark truncation. Screenshots go to `.webuddy/browser/*.png`, readable by the existing file tool. Browser state persists across calls within one terminal; terminal teardown removes its process tree and temporary profile.

Local checks:

```sh
node --test runtime/project-browser/bridge.test.mjs
.venv/bin/python -m pytest tests/test_project_browser.py -q
```

Real Linux acceptance (run from repository root as service user after installation):

```sh
PYTHONPATH=. .venv/bin/python runtime/project-browser/smoke.py
```

This starts a real preview inside the sandbox, opens mobile viewport, fills and clicks, checks rendered output, saves a real screenshot, and confirms an unrelated host port is rejected. No model calls or credentials are used.
