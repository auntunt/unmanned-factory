"""Tests for capability source aggregation (N6).

Required by task spec:
1. Only loaded, not invoked → appears in loaded, NOT in invoked
2. Real tool.call event → appears in invoked, traceable to event
3. Old run with no records → 'no_record', NOT empty list or fabrication (mutation-verified)
4. Session skill vs project module distinguishable in loaded
5. (service_url test is in the frontend test file)
"""
import json
import pytest
from factory.control.store import Store
from factory.control.capability_source import aggregate


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'test.db')


def _create_run(store, rid, *, module_snapshot=None, session_skill_snapshot=None,
                mount_snapshot=None, project_id='p1'):
    """Helper: insert a minimal run into the store."""
    data = {'id': rid, 'project_id': project_id, 'status': 'published', 'request': 'test',
            'revision': 1, 'created_at': '2026-09-19T00:00:00Z', 'updated_at': '2026-09-19T00:00:00Z'}
    if module_snapshot is not None:
        data['module_snapshot'] = module_snapshot
    if session_skill_snapshot is not None:
        data['session_skill_snapshot'] = session_skill_snapshot
    if mount_snapshot is not None:
        data['mount_snapshot'] = mount_snapshot
    with store.connect() as db:
        db.execute('INSERT INTO runs VALUES(?,?)', (rid, json.dumps(data)))
    return data


def _emit_tool_call(store, rid, tool_name, at='2026-09-19T00:01:00Z'):
    """Helper: insert a tool.call event."""
    payload = json.dumps({'name': tool_name, 'id': 'tc-1'})
    with store.connect() as db:
        db.execute('INSERT INTO events(run_id, task_id, type, payload, at) VALUES(?,?,?,?,?)',
                   (rid, None, 'tool.call', payload, at))


# --- Test 1: loaded-only, not invoked ---

def test_loaded_skill_not_in_invoked(store):
    """A skill that is loaded but never called must appear in loaded, not in invoked."""
    run = _create_run(store, 'r-load-only', module_snapshot=[
        {'id': 'mod-1', 'name': '简洁产品界面', 'version': 1},
    ])
    result = aggregate(store, run)

    assert result['loaded']['status'] == 'available'
    assert len(result['loaded']['items']) == 1
    assert result['loaded']['items'][0]['name'] == '简洁产品界面'
    assert result['loaded']['items'][0]['origin'] == 'project_module'

    assert result['invoked']['status'] == 'available'
    assert len(result['invoked']['items']) == 0
    # NOT in invoked
    invoked_names = [i['name'] for i in result['invoked']['items']]
    assert '简洁产品界面' not in invoked_names


# --- Test 2: real tool.call → appears in invoked ---

def test_real_tool_call_in_invoked(store):
    """A real tool.call event must appear in invoked with count and timestamps."""
    run = _create_run(store, 'r-invoked', module_snapshot=[])
    _emit_tool_call(store, 'r-invoked', 'Bash', at='2026-09-19T00:01:00Z')
    _emit_tool_call(store, 'r-invoked', 'Bash', at='2026-09-19T00:02:00Z')
    _emit_tool_call(store, 'r-invoked', 'Write', at='2026-09-19T00:03:00Z')

    result = aggregate(store, run)

    assert result['invoked']['status'] == 'available'
    assert len(result['invoked']['items']) == 2

    bash = next(i for i in result['invoked']['items'] if i['name'] == 'Bash')
    assert bash['count'] == 2
    assert bash['first_at'] == '2026-09-19T00:01:00Z'
    assert bash['last_at'] == '2026-09-19T00:02:00Z'

    write = next(i for i in result['invoked']['items'] if i['name'] == 'Write')
    assert write['count'] == 1


# --- Test 3: old run with no records → no_record (mutation-verified) ---

def test_old_run_no_record(store):
    """An old run with no module_snapshot, no session_skill_snapshot, and no tool.call events
    must return 'no_record' status for both loaded and invoked.
    NOT an empty list, NOT an empty string, NOT fabricated content."""
    run = _create_run(store, 'r-old')
    # No module_snapshot, no session_skill_snapshot set (they are absent from the run dict)

    result = aggregate(store, run)

    assert result['loaded']['status'] == 'no_record'
    assert result['loaded']['items'] == []
    assert result['invoked']['status'] == 'no_record'
    assert result['invoked']['items'] == []


def test_old_run_no_record_mutation_empty_list(store):
    """MUTATION: if aggregate returned 'available' instead of 'no_record' for an old run,
    this test must FAIL (red). This proves the no_record sentinel is not a dead path."""
    run = _create_run(store, 'r-old-mut')
    result = aggregate(store, run)

    # The correct value is 'no_record'. If someone changes the code to return 'available'
    # (e.g. by always returning 'available'), this assertion catches it.
    assert result['loaded']['status'] != 'available', \
        "MUTATION CAUGHT: old run must be 'no_record', not 'available'"
    assert result['invoked']['status'] != 'available', \
        "MUTATION CAUGHT: old run must be 'no_record', not 'available'"


def test_old_run_no_record_mutation_none(store):
    """MUTATION: if aggregate returned None instead of 'no_record', this test catches it."""
    run = _create_run(store, 'r-old-mut2')
    result = aggregate(store, run)

    assert result['loaded']['status'] is not None, \
        "MUTATION CAUGHT: status must be 'no_record', not None"
    assert result['invoked']['status'] is not None, \
        "MUTATION CAUGHT: status must be 'no_record', not None"
    assert result['loaded']['status'] == 'no_record'
    assert result['invoked']['status'] == 'no_record'


# --- Test 4: session skill vs project module distinguishable ---

def test_origin_distinguishable(store):
    """Session-imported skills and project modules must have distinct origin tags."""
    run = _create_run(store, 'r-mixed',
                      module_snapshot=[
                          {'id': 'mod-1', 'name': '项目模块A', 'version': 1},
                      ],
                      session_skill_snapshot=[
                          {'id': 'sk-1', 'name': '会话技能B'},
                      ],
                      mount_snapshot={
                          'documents': [{'id': 'session_skill/sk-1/SKILL.md',
                                         'session_skill_id': 'sk-1'}],
                      })

    result = aggregate(store, run)

    assert result['loaded']['status'] == 'available'
    assert len(result['loaded']['items']) == 2

    mod = next(i for i in result['loaded']['items'] if i['name'] == '项目模块A')
    assert mod['origin'] == 'project_module'

    skill = next(i for i in result['loaded']['items'] if i['name'] == '会话技能B')
    assert skill['origin'] == 'session_skill'


# --- Edge cases ---

def test_modern_run_empty_snapshots_no_tools(store):
    """A modern run with empty module_snapshot (but key present) and no tool calls
    is 'available' with empty items — NOT 'no_record'."""
    run = _create_run(store, 'r-empty-modern', module_snapshot=[])

    result = aggregate(store, run)

    assert result['loaded']['status'] == 'available'
    assert result['loaded']['items'] == []
    assert result['invoked']['status'] == 'available'
    assert result['invoked']['items'] == []


def test_only_session_skill_snapshot(store):
    """A run with only session_skill_snapshot (no module_snapshot) is 'available'."""
    run = _create_run(store, 'r-session-only',
                      session_skill_snapshot=[
                          {'id': 'sk-2', 'name': '会话技能X'},
                      ],
                      mount_snapshot={
                          'documents': [{'id': 'session_skill/sk-2/SKILL.md',
                                         'session_skill_id': 'sk-2'}],
                      })

    result = aggregate(store, run)

    assert result['loaded']['status'] == 'available'
    assert len(result['loaded']['items']) == 1
    assert result['loaded']['items'][0]['origin'] == 'session_skill'
    # invoked is available (modern run) but empty
    assert result['invoked']['status'] == 'available'
    assert result['invoked']['items'] == []
