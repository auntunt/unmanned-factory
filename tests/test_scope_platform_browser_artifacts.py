"""平台浏览器截图不应被当作未声明的业务修改。

现场（run 88256a6c…，候选 ebf0a64）：ledger 33 pass / 1 fail，唯一失败是
scope:reconciliation，未声明文件正是平台自己生成的两张
`.webuddy/browser/preview-<uuid>.png`。路径由平台 bridge 用随机 UUID 选定
（runtime/project-browser/bridge.mjs::screenshotPath），模型无从指定，并在
`browser.observed` 事件上逐张登记。

界定依据只能是这份平台登记，不能是「整个 .webuddy/」或「所有 PNG」——
否则伪装成截图路径的业务改动就能混过对账。
"""
import hashlib
import json
from types import SimpleNamespace

import pytest

from factory.control import scope_declaration as scope
from factory.control.store import Store
from tests.test_spec_tree import repo, command, commit  # noqa: F401


@pytest.fixture
def env(repo, tmp_path):
    store = Store(tmp_path / 'state.db')
    p = store.add_project({'name': 'app', 'workspace': str(repo), 'base_branch': 'main',
                           'spec_tree_enabled': True})
    run = store.create_run(p['id'], 'change')[0]
    store.update(run['id'], {'context': {'commit_sha': command(repo, 'rev-parse', 'HEAD')}})
    emit = scope.wrap_emit(store, run['id'],
                           SimpleNamespace(workspace=str(repo), read_only=False, verification=False),
                           lambda kind, data: store.append(run['id'], kind, data, 'task-1'))
    return repo, store, p, run['id'], emit


def _blob(data):
    return hashlib.sha1(b'blob %d\x00' % len(data) + data).hexdigest()


def _shot(root, store, rid, name, data=b'\x89PNG\r\n\x1a\n fake viewport', bind=True):
    """平台真的写一张截图，并按 browser.observed 登记路径 + 内容绑定。"""
    folder = root / '.webuddy' / 'browser'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    payload = {'action': 'screenshot', 'ok': True, 'url': 'http://127.0.0.1:8080/',
               'screenshot_path': str(path)}
    if bind:
        payload |= {'screenshot_sha256': hashlib.sha256(data).hexdigest(),
                    'screenshot_blob': _blob(data)}
    store.append(rid, 'browser.observed', payload, 'coding')
    return path


def _check(env):
    root, store, p, rid, _ = env
    return scope.evidence(store, rid, p, root, commit(root, 'changes'))[0]


def test_platform_screenshots_are_not_undeclared_changes(env):
    root, store, p, rid, emit = env
    emit('assistant.message', {'text': json.dumps({'scope_declaration': {
        'files': [{'path': 'app.py', 'spec_nodes': ['.spec/app/spec.md']}]}})})
    (root / 'app.py').write_text('x=2')
    _shot(root, store, rid, 'preview-75ad3b44-e111-4505-85a2-901ab502a7d4.png')
    _shot(root, store, rid, 'preview-c2214ac0-05b1-4b8c-85d0-dd64d38a2958.png')

    item = _check(env)
    assert item['status'] == 'pass', item
    assert item['undeclared_changes'] == [], item
    assert '.webuddy/browser/preview-75ad3b44-e111-4505-85a2-901ab502a7d4.png' in item['exempt_files']


def test_unrecorded_file_under_the_same_folder_is_still_undeclared(env):
    """伪装路径不豁免：平台没登记过的文件，哪怕放在同一目录、同样叫 preview-*.png。"""
    root, store, p, rid, emit = env
    _shot(root, store, rid, 'preview-real-platform.png')
    folder = root / '.webuddy' / 'browser'
    (folder / 'preview-deadbeef-cafe-4000-8000-000000000000.png').write_bytes(b'not from the platform')

    item = _check(env)
    assert item['status'] == 'fail', item
    assert item['undeclared_changes'] == [
        '.webuddy/browser/preview-deadbeef-cafe-4000-8000-000000000000.png'], item


def test_business_source_is_still_undeclared_even_with_screenshots_present(env):
    root, store, p, rid, emit = env
    _shot(root, store, rid, 'preview-abc.png')
    (root / 'sneaky.py').write_text('print("business change")')

    item = _check(env)
    assert item['status'] == 'fail', item
    assert item['undeclared_changes'] == ['sneaky.py'], item


def test_screenshot_path_from_another_run_is_not_exempt(env, tmp_path):
    """登记必须属于本次运行：别的 run 的登记不能豁免本 run 的改动。"""
    root, store, p, rid, emit = env
    other = store.create_run(p['id'], 'other')[0]
    folder = root / '.webuddy' / 'browser'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'preview-from-another-run.png'
    path.write_bytes(b'\x89PNG')
    store.append(other['id'], 'browser.observed',
                 {'action': 'screenshot', 'ok': True, 'screenshot_path': str(path)}, 'coding')

    item = _check(env)
    assert item['status'] == 'fail', item
    assert item['undeclared_changes'] == ['.webuddy/browser/preview-from-another-run.png'], item


def test_same_path_rewritten_after_production_is_still_undeclared(env):
    """同路径、内容被改写：平台登记的是那一刻的字节，不是这条路径的通行证。

    Codex 现场提醒：事件只证明平台曾生成该路径，不证明随后提交的字节没被换掉。
    """
    root, store, p, rid, emit = env
    path = _shot(root, store, rid, 'preview-abc.png')
    path.write_bytes(b'print("business code smuggled through a screenshot path")')

    item = _check(env)
    assert item['status'] == 'fail', item
    assert item['undeclared_changes'] == ['.webuddy/browser/preview-abc.png'], item


def test_legacy_event_without_content_binding_is_not_exempt(env):
    """旧事件没有内容绑定：失败保守，不豁免，也绝不事后补造哈希。"""
    root, store, p, rid, emit = env
    _shot(root, store, rid, 'preview-legacy.png', bind=False)

    item = _check(env)
    assert item['status'] == 'fail', item
    assert item['undeclared_changes'] == ['.webuddy/browser/preview-legacy.png'], item
