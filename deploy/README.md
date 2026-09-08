# v2 runtime update

The existing deployment uses `factoryweb.service`, project checkout
`/home/ubuntu/workspace/unmannedfactory`, and Caddy origin
`https://harness.cloudwaveai.cn`. Preserve its actual external EnvironmentFile,
control data directory, service user, unit and Caddy configuration. Do not
create another service on port 8788. The `factory-control.service` file is an
example for a new host, not a replacement for the existing unit.

The v2 gateway starts SDK children for individual tasks; it does not require a
long-lived agent daemon. Legacy `factoryapi` / `factory-api`, dashboard and
worker units must not be activated for this deployment.

## Inspect and update

Inspect only non-secret systemd metadata; do not print `Environment`, full
unit files, credentials or authentication files:

```sh
systemctl show factoryweb.service -p FragmentPath -p User -p WorkingDirectory -p EnvironmentFiles
systemctl is-active factoryweb.service
```

Find the existing control data directory locally without copying environment
values into a support transcript. Run the installer as the service account,
using the actual existing paths (the angle-bracket values below must be filled):

```sh
cd /home/ubuntu/workspace/unmannedfactory
bash deploy/install-control.sh --checkout "$PWD" --user ubuntu \
  --env-file '<existing EnvironmentFile path>' \
  --data-dir '<existing FACTORY_CONTROL_DATA directory>' \
  --unit-file '<existing factoryweb.service FragmentPath>'
```

Repeat with `--apply` for the already authorized deployment update. It runs
`uv sync --frozen --all-extras --no-dev`, `npm ci`, and
`node ./node_modules/vite/bin/vite.js build` in the correct directories. It
runs doctor against a temporary read-only snapshot of the existing database.
It does not modify the live database, external env file, unit, Caddy, firewall
or apt state. It does not activate any service. Running apply as root is
rejected to avoid changing ownership of SDKs, assets and authentication state.

Before rebuilding an active deployment, retain the previous reviewed commit
and frontend build for rollback. Preserve existing local edits; deploy an
explicit reviewed commit, not an assumed moving branch. Restart the existing
unit after checking the build:

```sh
sudo systemctl restart factoryweb.service
systemctl is-active factoryweb.service
curl -fsS -o /dev/null http://127.0.0.1:8788/
```

Do not run `uv run` at service startup: dependency resync can remove optional
SDK extras. The existing unit should use the checkout's `.venv/bin/factory-web
serve` directly. If its command differs, prepare a targeted unit adjustment
that preserves the external EnvironmentFile and all actual deployment paths.
Never replace an existing environment file with the example or an empty file.

## Doctor and connection tests

```sh
.venv/bin/python -m factory.control.runtime_cli doctor --json \
  --workspace "$PWD" --static-dir "$PWD/frontend/dist" \
  --db '<existing FACTORY_CONTROL_DATA directory>/control.db'
```

Doctor checks SDK import/API compatibility and the executable resolved by each
SDK, including bundled runtimes. It reports credential presence as a hint,
without reading credential-file contents. The current CLI environment may
 differ from systemd's environment; use the authenticated runtime page for the
service's actual configuration. Doctor makes no model request and never seeds
or migrates the live database.

Saved model roles take precedence over environment seed values after first
startup. Configure the four actual model IDs through the runtime page. A
successful read-only connection test is a timestamped observation, not a
promise of future availability or write capability. DSH cannot be tested by
this read-only probe. GitHub token presence does not prove publish permission.
Unknown model costs remain unknown; bounded execution requires the project's
explicit budget policy and does not enable automatic publication.

## New hosts only

Use `factory-control.service` and `control.env.example` as reviewed templates.
Create a new external mode-0600 EnvironmentFile only after checking it does
not already exist. Adjust user, checkout, data and workspace locations to the
host. Configure the HTTPS origin to match Caddy exactly. Use the same data
directory when creating the first owner with `factory-web create-user`; read
the password interactively. The app supplies authentication and CSRF; extra
Caddy basic-auth is not required. Review OS sandbox compatibility with the
chosen SDK before enabling a new service. SDK sandboxing and a web login do
not provide separate OS identities for the control plane and workers.

See [HERMES-HANDOFF.md](HERMES-HANDOFF.md) for local operator assistance.
