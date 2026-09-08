"""Opt-in real SDK tool check with a fresh file value the model must read."""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factory.control.auth import AuthStore
from factory.control.governance import Governance, GovernedRunner
from factory.control.providers import ProviderRequest, SDKRunner
from factory.control.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--model', required=True)
    args = parser.parse_args()
    if not args.execute:
        parser.error('--execute is required: this check invokes the real model')
    folder = ROOT / '.factory-preview' / ('live-command-read-' + uuid.uuid4().hex[:10])
    workspace = folder / '工具读取'
    workspace.mkdir(parents=True)
    nonce = uuid.uuid4().hex
    (workspace / 'probe.txt').write_text(nonce + '\n')
    auth = AuthStore(folder / 'users.db')
    owner = auth.create_user('acceptance', uuid.uuid4().hex)
    governance = Governance(auth, Store(folder / 'control.db'))
    governance.set_reservation(30000, 'acceptance')
    governance.set_limit('workspace', 'all', 100000, 'acceptance')
    events = []
    runner = GovernedRunner(SDKRunner(), governance, actor_id=owner['id'])
    print(json.dumps({'data_dir': str(folder), 'model': args.model}), flush=True)
    result = runner.run(ProviderRequest(provider='codex', model=args.model, workspace=str(workspace),
        timeout_s=90, read_only=True, prompt='Read probe.txt in the current working directory using a shell command. '
        'Return exactly its contents and nothing else. Do not edit files, infer contents, or perform unrelated work. '
        'One successful command is sufficient.'), lambda typ, payload: events.append({'type': typ, 'payload': payload}))
    commands = [e for e in events if e['type'] == 'tool.call' and e['payload'].get('name') == 'command']
    outputs = [e for e in events if e['type'] == 'tool.result'
        and nonce in str(e['payload'].get('output', '')) and e['payload'].get('exit_code') == 0]
    passed = result.text.strip() == nonce and bool(commands) and bool(outputs)
    report = {'passed': passed, 'provider': 'codex', 'model': args.model, 'session_id': result.session_id,
        'workspace_contains_unicode': not str(workspace).isascii(),
        'model_read_expected_nonce': result.text.strip() == nonce,
        'observed_command_calls': len(commands), 'observed_successful_command_results': len(outputs),
        'tokens_in': result.tokens_in, 'tokens_out': result.tokens_out,
        'cached_input_tokens': result.cached_input_tokens, 'cost_usd': result.cost_usd,
        'quota_calls': governance.summary(owner)['calls']}
    (folder / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    (folder / 'events.json').write_text(json.dumps(events, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
