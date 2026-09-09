import json
import subprocess
from pathlib import Path
import threading

import pytest

from factory.control.continuous import execute_continuous
from factory.control.continuous_migration import migrate_to_continuous
from factory.control.execution import ExecutionError
from factory.control.providers import ProviderResult


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


@pytest.fixture
def scene(tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'user.name', 'Test'); git(root, 'config', 'user.email', 'test@example.com')
    (root / 'base.txt').write_text('baseline\n'); git(root, 'add', '.'); git(root, 'commit', '-qm', 'baseline')
    base = git(root, 'rev-parse', 'HEAD')
    def tree(name, ref):
        path = tmp_path / name
        git(root, 'worktree', 'add', '-q', '-b', name, str(path), ref)
        return path
    first = tree('first', base); (first / 'first.py').write_text('first = 1\n')
    git(first, 'add', '.'); git(first, 'commit', '-qm', 'first'); c1 = git(first, 'rev-parse', 'HEAD')
    second = tree('second', c1); (second / 'second.py').write_text('second = 2\n')
    git(second, 'add', '.'); git(second, 'commit', '-qm', 'second'); c2 = git(second, 'rev-parse', 'HEAD')
    draft = tree('draft', c2); (draft / 'base.txt').write_text('baseline with draft\n'); (draft / 'third.py').write_text('third = 3\n')
    plan = {'title':'all requirements', 'summary':'maintain', 'tasks':[
        {'id': 'third', 'prompt': 'finish third feature', 'acceptance':['third accepts CSV'], 'depends_on':['second'], 'risk':'low'},
        {'id': 'second', 'prompt': 'second feature', 'acceptance':['second produces report'], 'depends_on':['first'], 'risk':'low'},
        {'id': 'first', 'prompt': 'first feature', 'acceptance':['first parses files'], 'depends_on':[], 'risk':'low'}]}
    artifacts = {'base_sha':base, 'tasks':[
        {'id':'first','status':'verified','commit':c1,'worktree':str(first),'branch':'first'},
        {'id':'second','status':'verified','commit':c2,'worktree':str(second),'branch':'second'},
        {'id':'third','status':'cancelled','commit':None,'worktree':str(draft),'branch':'draft','session_id':'old-session'}]}
    project = {'workspace':str(root),'base_branch':'main','checks':{'integrity':['git','diff','--check','HEAD']}}
    profiles = {'standard':{'provider':'claude','model':'m'},'strong':{'provider':'claude','model':'m'}}
    return project, profiles, plan, artifacts, draft


def migrate(scene):
    project, profiles, plan, artifacts, _ = scene
    return migrate_to_continuous(run_id='legacy', plan=plan, project=project, profiles=profiles, artifacts=artifacts)


def test_verified_commits_and_dirty_draft_preserved_and_executor_can_resume(scene):
    project, profiles, old_plan, original, draft = scene
    source_refs = {state['branch']:git(Path(state['worktree']), 'rev-parse','HEAD') for state in original['tasks']}
    before_draft = git(draft, 'diff', 'HEAD')
    plan, artifacts = migrate(scene)
    root = Path(artifacts['worktree'])
    assert (root / 'first.py').read_text() == 'first = 1\n'
    assert (root / 'second.py').read_text() == 'second = 2\n'
    assert (root / 'third.py').read_text() == 'third = 3\n'
    assert (root / 'base.txt').read_text() == 'baseline with draft\n'
    assert artifacts['session_id'] is None and artifacts['commit'] is None
    assert artifacts['migration']['completed_task_ids'] == ['first','second']
    assert artifacts['migration']['restored_drafts'] == ['third']
    for task in old_plan['tasks']:
        assert task['prompt'] in plan['tasks'][0]['prompt']
        assert task['acceptance'][0] in plan['tasks'][0]['acceptance']
    assert git(Path(project['workspace']), 'rev-parse', 'main') == original['base_sha']
    for state in original['tasks']:
        assert git(Path(state['worktree']), 'rev-parse', 'HEAD') == source_refs[state['branch']]
    assert git(draft, 'diff','HEAD') == before_draft
    class Runner:
        def run(self, request, emit, cancel=None):
            assert request.workspace == str(root) and request.session_id is None
            return ProviderResult('draft verified', session_id='new-session')
    result = execute_continuous(run_id='migrated', plan=plan, project=project, profiles=profiles,
        runner=Runner(), emit=lambda *_:None, cancel=threading.Event(), resume_artifacts=artifacts)
    assert result['tasks'][0]['status'] == 'verified'
    assert git(Path(project['workspace']), 'rev-parse','main') == original['base_sha']


def test_untracked_draft_conflict_preserves_both_sources_and_destination(scene):
    project, _, _, original, draft = scene
    # Two unfinished sources both create a new file; no last-writer-wins overwrite.
    other = Path(project['workspace']).parent / 'other'
    git(Path(project['workspace']), 'worktree','add','-q','-b','other',str(other),original['base_sha'])
    (other/'third.py').write_text('conflicting other draft\n')
    scene[2]['tasks'].append({'id':'fourth','prompt':'fourth','acceptance':['fourth'], 'depends_on':[], 'risk':'low'})
    original['tasks'].append({'id':'fourth','status':'failed','worktree':str(other),'branch':'other'})
    with pytest.raises(ExecutionError, match='conflicts') as failure:
        migrate(scene)
    destination = Path(failure.value.artifacts['worktree'])
    assert (destination/'third.py').read_text() == 'third = 3\n'
    assert (other/'third.py').read_text() == 'conflicting other draft\n'
    assert (draft/'third.py').read_text() == 'third = 3\n'
    assert (destination.parent/'migration-report.json').exists()
    assert failure.value.artifacts['migration']['status'] == 'conflict_or_failed'
    assert git(Path(project['workspace']), 'rev-parse','main') == original['base_sha']


def test_verified_commit_conflict_stops_in_new_worktree(scene):
    project, _, plan, original, _ = scene
    root = Path(project['workspace']); conflict = root.parent/'conflict'
    git(root, 'worktree','add','-q','-b','conflict',str(conflict),original['base_sha'])
    (conflict/'first.py').write_text('different implementation\n'); git(conflict,'add','.'); git(conflict,'commit','-qm','conflict')
    plan['tasks'][1]['depends_on'] = []
    original['tasks'][1].update(commit=git(conflict,'rev-parse','HEAD'), worktree=str(conflict), branch='conflict')
    with pytest.raises(ExecutionError, match='preserved') as failure:
        migrate(scene)
    destination = Path(failure.value.artifacts['worktree'])
    assert git(destination, 'diff', '--name-only','--diff-filter=U') == 'first.py'
    assert (conflict/'first.py').read_text() == 'different implementation\n'
    assert git(root,'rev-parse','main') == original['base_sha']


def test_unverified_committed_work_is_not_silently_lost(scene):
    draft = scene[-1]
    git(draft,'add','.'); git(draft,'commit','-qm','unverified')
    with pytest.raises(ExecutionError, match='unverified commits'):
        migrate(scene)


def test_changed_baseline_and_incomplete_verified_dependencies_rejected(scene):
    root = Path(scene[0]['workspace'])
    (root/'new.txt').write_text('changed'); git(root,'add','.'); git(root,'commit','-qm','changed')
    with pytest.raises(ExecutionError, match='baseline changed'):
        migrate(scene)


def test_cycle_or_verified_task_with_unfinished_dependency_rejected(scene):
    scene[2]['tasks'][2]['depends_on'] = ['third']
    with pytest.raises(ExecutionError, match='cyclic'):
        migrate(scene)
    scene[2]['tasks'][2]['depends_on'] = []
    scene[3]['tasks'][0]['status'] = 'cancelled'
    with pytest.raises(ExecutionError, match='unfinished dependency'):
        migrate(scene)


def test_source_branch_identity_change_rejected_without_mutation(scene):
    scene[3]['tasks'][2]['branch'] = 'wrong-branch'
    draft_before = git(scene[-1], 'diff','HEAD')
    with pytest.raises(ExecutionError, match='identity changed'):
        migrate(scene)
    assert git(scene[-1], 'diff','HEAD') == draft_before


def test_integration_extra_commit_is_not_silently_dropped(scene):
    draft = scene[-1]
    git(draft,'add','.'); git(draft,'commit','-qm','integration-extra')
    scene[3]['current_commit'] = git(draft,'rev-parse','HEAD')
    with pytest.raises(ExecutionError, match='additional commits'):
        migrate(scene)


def test_verified_source_dirty_changes_are_not_silently_dropped(scene):
    source = Path(scene[3]['tasks'][0]['worktree'])
    (source/'uncommitted.py').write_text('keep me')
    with pytest.raises(ExecutionError, match='dirty draft'):
        migrate(scene)
    assert (source/'uncommitted.py').read_text() == 'keep me'
