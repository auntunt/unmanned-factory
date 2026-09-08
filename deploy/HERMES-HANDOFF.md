# Hermes handoff for the existing deployment

Run this on the already authorized server as the existing service account.
No SSH key, public SSH address or credential contents belong in this document
or a Hermes transcript. Hermes is an operator assistant, not a fourth SDK.

The known service is `factoryweb.service`, checkout
`/home/ubuntu/workspace/unmannedfactory`, and public HTTPS origin
`https://harness.cloudwaveai.cn`. Confirm these against the actual machine;
preserve its external EnvironmentFile, data directory and Caddy configuration.
Never activate `factoryapi`, `factory-api` or a second listener on port 8788.

First discover the installed CLI interface:

```sh
command -v hermes
hermes --help
hermes chat --help
```

If the installed `hermes chat --help` documents `--query-file`, the following
commands create a private, non-secret instruction file and invoke that form.
Otherwise pass these instructions through the actually supported interface;
do not guess flags:

```sh
umask 077
handoff_path=$(mktemp /tmp/factory-hermes-handoff.XXXXXX)
cat > "$handoff_path" <<'REQUEST'
Continue the authorized Unmanned Factory deployment locally. Work as the
existing service account on /home/ubuntu/workspace/unmannedfactory.
Never print environment contents, unit contents, tokens, login files, private
keys or raw exception output that may include credentials.

1. Check current worktree status and intended repository/refs. Preserve user
changes. Deploy only the exact reviewed commit supplied by the operator; if
that commit is absent, report the current revision and prepare the next step.
Do not reset, stash, overwrite user edits or invent a commit.
2. Inspect non-secret metadata with:
   systemctl show factoryweb.service -p FragmentPath -p User -p WorkingDirectory -p EnvironmentFiles
   systemctl is-active factoryweb.service
Resolve the existing control data path locally without echoing secret values.
Confirm the actual HTTPS origin is https://harness.cloudwaveai.cn; preserve
Caddy and the protected external EnvironmentFile. Never source that file as
shell code or copy its contents into prompts.
3. Retain the prior commit/build for rollback. Run deploy/install-control.sh
with the actual checkout, service user, external env path, control data path
and existing unit path. Run its dry-run first, then --apply for the authorized
update. Do not build as root. This updater never changes the unit/env/Caddy.
4. The direct service entry is .venv/bin/factory-web serve. If the existing
unit needs a change, prepare only that change while retaining all external
settings. Never install a second factory-control service over factoryweb.
5. Doctor is local-only and uses a temporary database snapshot. A shell doctor
may lack systemd's credentials. After the reviewed build, restart the existing
factoryweb.service within the deployment authorization and inspect the
runtime page through its authenticated API/UI. Do not print login credentials.
6. Configure planner/cheap/standard/strong with actual account-supported model
IDs. SDK installed, bundled runtime executable, authentication hint, and live
connection test are separate facts. Use only explicit fixed read-only probes;
never infer live success from import. DSH has no verified read-only probe.
7. Report installed commit, build/doctor results, service activation and actual
probe outcomes. Report any unavailable access honestly. Do not bypass network
or OS access controls. Never start legacy factoryapi/factory-api/dashboard/loop.
REQUEST
hermes chat --query-file "$handoff_path"
rm -f "$handoff_path"
```

Supply the exact reviewed commit with the handoff before applying an update.
The commands do not include secrets; do not append provider credentials or SSH
material to them. Use locally protected operator configuration for credentials.
