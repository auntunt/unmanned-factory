"""Opt-in, billable acceptance of the real factory SDK/graph in an isolated repo.

Run with --execute --model <an account-supported Codex model>. Uses existing
provider authentication and proxy environment. Never publishes or deploys.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factory.control.auth import AuthStore
from factory.control.autonomy import DEFAULT_POLICY, PolicyStore
from factory.control.governance import Governance
from factory.control.service import Service
from factory.control.store import Store, scrub
from factory.control.autonomy_routes import evidence_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Acknowledge real provider usage')
    parser.add_argument('--model', required=True)
    parser.add_argument('--worker-model')
    parser.add_argument('--max-risk', choices=('low', 'medium', 'high'), default='medium')
    parser.add_argument('--timeout', type=int, default=240)
    args = parser.parse_args()
    if not args.execute:
        parser.error('--execute is required: this acceptance invokes the real model')
    if not 30 <= args.timeout <= 600:
        parser.error('--timeout must be 30–600 seconds per phase')
    data = ROOT / '.factory-preview' / ('live-qualification-' + uuid.uuid4().hex[:10])
    repo = data / 'workspaces' / 'ticket-routing'
    repo.mkdir(parents=True)
    for cmd in (['init', '-q', '-b', 'main'], ['config', 'user.name', 'Factory acceptance'], ['config', 'user.email', 'acceptance@localhost.invalid']):
        subprocess.run(['git', *cmd], cwd=repo, check=True, capture_output=True)
    (repo / 'ticket_rules.py').write_text('def classify_ticket(title, impacted_users=1, security_incident=False):\n    return "normal"\n')
    (repo / 'README.md').write_text('# Ticket routing acceptance\n\nImplement ticket_rules.classify_ticket and ticket_summary.summarize_ticket. Project checks are controlled by the factory.\n')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'Seed independent acceptance'], cwd=repo, check=True)
    base_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    rules = "from ticket_rules import classify_ticket as f; assert f('question') == 'normal'; assert f('question', 50) == 'normal'; assert f('question', 51) == 'high'; assert f('OUTAGE') == 'high'; assert f('无法登录') == 'high'; assert f('outage', 100, True) == 'critical'; assert f('question', security_incident=True) == 'critical'"
    summary = "from ticket_summary import summarize_ticket as f; assert f('question') == {'priority':'normal','owner':'support'}; assert f('outage') == {'priority':'high','owner':'oncall'}; assert f('question', 51) == {'priority':'high','owner':'oncall'}; assert f('question', security_incident=True) == {'priority':'critical','owner':'security'}"
    store = Store(data / 'control.db')
    project = store.add_project({'name': 'Live ticket routing acceptance', 'repository': 'acceptance/ticket-routing', 'workspace': str(repo),
        'base_branch': 'main', 'checks': {'routing': [sys.executable, '-c', rules], 'summary': [sys.executable, '-c', summary]},
        'budget_usd': 5.0, 'auto_issues': False, 'auto_publish': False})
    auth = AuthStore(data / 'users.db')
    # No server is opened and the random acceptance password is never output.
    owner = auth.create_user('acceptance', uuid.uuid4().hex)
    governance = Governance(auth, store)
    governance.set_reservation(60000, 'acceptance')
    governance.set_limit('workspace', 'all', 400000, 'acceptance')
    governance.set_limit('member', str(owner['id']), 400000, 'acceptance')
    profiles = {role: {'provider': 'codex', 'model': args.model if role == 'planner' else (args.worker_model or args.model)}
                for role in ('planner', 'cheap', 'standard', 'strong')}
    service = Service(store, profiles=profiles, timeout_s=args.timeout, max_parallel=2)
    service.governance = governance
    config = service.runtime_settings.get()
    service.runtime_settings.update({'profiles': profiles, 'limits': {**config['limits'], 'max_tasks': 3, 'unknown_cost_policy': 'allow_bounded'}}, config['revision'], 'acceptance')
    PolicyStore(store).update(project['id'], {**DEFAULT_POLICY, 'mode': 'autonomous', 'max_risk': args.max_risk, 'max_attempts': 2}, 0, 'acceptance')
    goal = ('Implement the following fully specified local Python feature without asking questions. '
        'In ticket_rules.py implement classify_ticket(title, impacted_users=1, security_incident=False): '
        'return critical if security_incident is true; otherwise high when impacted_users > 50 or '
        'title contains outage (case insensitive) or 无法登录; otherwise normal. '
        'In new ticket_summary.py implement summarize_ticket with the same arguments, reusing classify_ticket, '
        'returning a dict with priority and owner. Map critical to security, high to oncall, normal to support. '
        'Only these two source files may change. No dependencies, UI, network, commits or deployment. '
        'Plan two tasks: rules checked with routing, and summary depending on rules checked with summary. '
        'Use the existing named checks without changing check infrastructure.')
    run, _ = store.create_run(project['id'], goal, source={'type': 'web', 'actor': owner['username'], 'actor_id': owner['id']})
    print(json.dumps({'run_id': run['id'], 'data_dir': str(data), 'models': profiles}, ensure_ascii=False), flush=True)
    previous = None
    try:
        service.start_plan(run['id'])
        deadline = time.monotonic() + args.timeout * 2 + 30
        while time.monotonic() < deadline:
            current = store.get(run['id'])
            if current['status'] != previous:
                print(json.dumps({'status': current['status'], 'run_id': run['id']}, ensure_ascii=False), flush=True)
                previous = current['status']
            if current['status'] not in ('received', 'planning', 'queued', 'running', 'verifying'):
                # Policy authorization briefly passes through awaiting_approval
                # inside the active planning job. Do not shut down its worker.
                with service.lock:
                    active = run['id'] in service.active_jobs
                if not active:
                    break
            time.sleep(1)
        else:
            service.cancel(run['id'], 'acceptance timeout')
    finally:
        service.close()
    final = store.get(run['id'])
    bundle = evidence_bundle(store, run['id'])
    (data / 'evidence.json').write_text(json.dumps(scrub(bundle), ensure_ascii=False, indent=2))
    unchanged = (not subprocess.check_output(['git', 'status', '--porcelain'], cwd=repo, text=True)
        and subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip() == base_commit)
    result = {'status': final['status'], 'base_checkout_unchanged': unchanged,
        'commit': final['artifacts'].get('commit'), 'checks': final['artifacts'].get('checks', []),
        'quota': governance.summary(owner)['workspace'], 'provider_calls': governance.summary(owner)['calls'],
        'evidence': str(data / 'evidence.json')}
    (data / 'result.json').write_text(json.dumps(scrub(result), ensure_ascii=False, indent=2))
    print(json.dumps(scrub(result), ensure_ascii=False), flush=True)
    return 0 if final['status'] == 'ready_for_review' and unchanged else 1


if __name__ == '__main__':
    raise SystemExit(main())
