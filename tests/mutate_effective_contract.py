"""Are the new gates actually load-bearing? One mutation per run, always restored.

Each entry replaces one substring in production code and asserts the replacement
really landed (a silent miss looks exactly like a surviving mutant). The suite
must then go red, and the baseline pass count must not shrink.

    python tests/mutate_effective_contract.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITE = ['tests/test_effective_contract.py', 'tests/test_run_followup.py',
         'tests/test_auto_consume.py']

MUTATIONS = [
    ('factory/control/acceptance_ledger.py',
     'effective_contract.criteria_texts(contract)',
     "effective_contract.criteria_texts({**contract, 'superseded_non_goals': []})",
     'acceptance keeps a forbidden zone the owner lifted'),
    ('factory/control/effective_contract.py',
     "        if index >= len(non_goals) or non_goals[index] != change['quote']:\n"
     "            raise ValueError('变更分析引用了规格中不存在的非目标边界')",
     '        if False:\n            pass',
     'a made-up forbidden zone can be lifted'),
    ('factory/control/effective_contract.py',
     "    if revision is not None and contract['revision'] != revision:",
     '    if False:',
     'work bound to an old agreement is handed a newer one'),
    # Put the receipt back outside the revision's transaction, the way it was.
    # The anchor carries continue_run's own event line, because the same call
    # shape also appears in _auto_resume_with_followups (inside a try block,
    # where a bare insertion would be a SyntaxError rather than a mutation).
    ('factory/control/run_lifecycle.py',
     "                'revision': revision, 'resume_count': resume_count + 1}),\n"
     '            events=_applied_events(run, collected, contract=contract))',
     "                'revision': revision, 'resume_count': resume_count + 1}))\n"
     '        _mark_followups_applied(self, rid, collected)',
     'the receipt leaves the revision transaction (continue_run)'),
    ('factory/control/run_lifecycle.py',
     '    revised = contract\n',
     '    revised = contract\n    _ = 0\n',
     'CONTROL: an inert edit must NOT redden the suite'),
    ('factory/control/run_lifecycle.py',
     "        if analysis['unresolved']:\n"
     "            svc.store.append(rid, 'contract.unresolved', {\n"
     "                'pending_id': pending['id'], 'questions': analysis['unresolved']})\n"
     '            continue',
     "        if analysis['unresolved']:\n"
     "            svc.store.append(rid, 'contract.unresolved', {\n"
     "                'pending_id': pending['id'], 'questions': analysis['unresolved']})",
     'an undecidable business conflict is applied anyway'),
]


def run_suite():
    proc = subprocess.run([sys.executable, '-m', 'pytest', *SUITE, '-q', '-p', 'no:randomly',
                           '-m', 'not smoke'], cwd=ROOT, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip().splitlines()[-1]


def main():
    code, summary = run_suite()
    if code != 0:
        print(f'BASELINE RED, nothing to measure: {summary}')
        return 1
    baseline = int(summary.split()[0])
    print(f'baseline: {summary}')
    survivors = []
    for path, old, new, label in MUTATIONS:
        target = ROOT / path
        source = target.read_text()
        assert old in source, f'mutation never landed ({path}): {label}'
        try:
            target.write_text(source.replace(old, new, 1))
            code, summary = run_suite()
        finally:
            target.write_text(source)
        # A control entry proves the harness is measuring the mutation and not
        # some unrelated flakiness: it must survive, and a red control is a
        # broken harness rather than a finding.
        control = label.startswith('CONTROL')
        killed = code != 0
        ok = killed != control
        print(f"{'ok' if ok else 'PROBLEM'} [{'killed' if killed else 'survived'}]: "
              f'{label} -- {summary}')
        if not ok:
            survivors.append(label)
    code, summary = run_suite()
    assert code == 0 and int(summary.split()[0]) == baseline, f'restore failed: {summary}'
    print('restored:', summary)
    if survivors:
        print('SURVIVORS:', *survivors, sep='\n  ')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
