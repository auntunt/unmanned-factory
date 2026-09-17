"""复核修复的真实回归：隔离、依赖探测、schema、进程组、耐久任务与幂等。

这些用例跑的是执行层本身——真的起子进程、真的在沙箱里跑、真的等超时回收，
不是在存储层模拟状态。
"""
import json
import os
import resource
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

import pytest

from factory.control import pack_runtime, pack_sandbox
from factory.control.capability_packs import PackStore
from factory.control.pack_runtime import (environment_report, evaluate, memory_limit_supported,
                                          run_tool, validate_instance)

BASE_SCHEMA_IN = {'type': 'object', 'required': ['input_dir', 'output_dir', 'inputs'],
                  'properties': {'input_dir': {'type': 'string'}, 'output_dir': {'type': 'string'},
                                 'inputs': {'type': 'array'}, 'options': {'type': 'object'}}}
BASE_SCHEMA_OUT = {'type': 'object', 'required': ['status'],
                   'properties': {'status': {'type': 'string', 'enum': ['ok', 'error']},
                                  'outputs': {'type': 'array'}, 'result': {'type': 'object'}}}


def version(body, *, timeout=10, input_schema=None, output_schema=None, max_output=8 << 20):
    files = {'tool/main.py': textwrap.dedent(body).encode()}
    return ({'manifest': {'tool': {'name': 't', 'entrypoint': 'tool/main.py', 'runtime': 'python3',
                                   'timeout_seconds': timeout,
                                   'input_schema': input_schema or BASE_SCHEMA_IN,
                                   'output_schema': output_schema or BASE_SCHEMA_OUT,
                                   'permissions': {'network': False, 'max_input_bytes': 8 << 20,
                                                   'max_output_bytes': max_output}},
                          'dependency_lock': {'python': '3', 'packages': []},
                          'support_matrix': [{'format': 'x', 'status': 'supported', 'evidence': ''}],
                          'evaluation_policy': {'test_set': 'fixtures/tests.json', 'required': True}}},
            files)


# ---- P0-1 服务进程不受影响 --------------------------------------------------
def test_importing_the_runtime_never_touches_the_service_process_limits():
    """探测能力必须在短命子进程里做。

    早先版本在模块导入时于服务进程先降后升 rlimit：Linux 上恢复会抛
    ValueError: not allowed to raise maximum limit，等于把服务进程永久降级。
    """
    watched = [name for name in ('RLIMIT_AS', 'RLIMIT_DATA', 'RLIMIT_CPU', 'RLIMIT_FSIZE', 'RLIMIT_NOFILE')
               if getattr(resource, name, None) is not None]
    before = {name: resource.getrlimit(getattr(resource, name)) for name in watched}
    import importlib
    importlib.reload(pack_runtime)
    assert pack_runtime.memory_limit_supported() in (True, False)  # 真的探测过
    after = {name: resource.getrlimit(getattr(resource, name)) for name in watched}
    assert after == before, f'服务进程的 rlimit 被改动了：{before} -> {after}'


def test_memory_probe_runs_in_a_child_process_only(monkeypatch):
    calls = []
    real = subprocess.run

    def spy(argv, *args, **kwargs):
        calls.append((argv, kwargs))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(pack_runtime, '_memory_supported', None)
    monkeypatch.setattr(pack_runtime.subprocess, 'run', spy)
    pack_runtime.memory_limit_supported()
    assert calls, '根本没有探测'
    argv, kwargs = calls[0]
    assert argv[0] == sys.executable and '-I' in argv
    # 探测子进程不继承任何凭据环境：显式给一个最小 env，而不是继承 os.environ。
    assert set(kwargs['env']) == {'PATH'}


# ---- P0-2 依赖声明只解析，不执行 --------------------------------------------
def test_dependency_declaration_is_parsed_never_executed(tmp_path):
    marker = tmp_path / 'executed.txt'
    hostile = f"os;open({str(marker)!r},'w').write('probe')#"
    report = environment_report({'dependency_lock': {'python': '3', 'packages': [hostile]}})
    assert not marker.exists(), '清单里的依赖字符串被当代码执行了'
    assert report['status'] == 'unavailable'
    assert any('依赖声明不合法' in problem for problem in report['problems'])


def test_dependency_versions_are_checked_against_installed_metadata():
    ok = environment_report({'dependency_lock': {'python': '3', 'packages': ['jsonschema>=4.0']}})
    assert ok['missing'] == [] and ok['status'] == 'ready'
    absent = environment_report({'dependency_lock': {'python': '3', 'packages': ['no-such-package-xyz']}})
    assert absent['status'] == 'unavailable' and absent['missing'] == ['no-such-package-xyz']
    # 版本约束不能被忽略：装着但版本不满足，同样是缺失。
    too_new = environment_report({'dependency_lock': {'python': '3', 'packages': ['jsonschema>=9999']}})
    assert too_new['status'] == 'unavailable' and '已安装' in too_new['missing'][0]


def test_python_constraint_compares_the_full_version_not_just_major():
    assert environment_report({'dependency_lock': {'python': '3', 'packages': []}})['status'] == 'ready'
    impossible = environment_report({'dependency_lock': {'python': '3.99', 'packages': []}})
    assert impossible['status'] == 'unavailable'
    assert any(item.startswith('python ') for item in impossible['missing'])
    ranged = environment_report({'dependency_lock': {'python': '>=3.12,<4', 'packages': []}})
    assert ranged['status'] == 'ready'


# ---- P0-3 没有可验证的隔离就不执行 ------------------------------------------
def test_isolation_is_verified_by_a_canary_not_assumed():
    result = pack_sandbox.probe(refresh=True)
    if not result.available:
        pytest.skip(f'本机没有可验证的隔离：{result.reason}')
    assert result.evidence['wrote_outside'] is False
    assert result.evidence['read_outside'] is False
    assert result.evidence['wrote_output'] is True


def test_without_verified_isolation_the_tool_is_refused_not_run(monkeypatch, tmp_path):
    marker = tmp_path / 'ran.txt'
    unavailable = pack_sandbox.Isolation(None, False, '测试注入：本机无隔离', {})
    monkeypatch.setattr(pack_runtime, 'probe', lambda **_: unavailable)
    v, files = version(f"""
        import json, sys
        from pathlib import Path
        Path({str(marker)!r}).write_text('ran')
        json.load(sys.stdin)
        print(json.dumps({{'status': 'ok', 'result': {{'a': 1}}}}))
    """)
    outcome = run_tool(v, files, [])
    assert outcome['status'] == 'failed' and outcome['error_code'] == 'isolation_unavailable'
    assert not marker.exists(), '没有隔离却把工具跑起来了'
    report = environment_report(v['manifest'])
    assert report['status'] == 'unavailable' and report['isolation_verified'] is False


def test_the_tool_cannot_read_host_files_outside_the_sandbox(tmp_path):
    if not pack_sandbox.probe().available:
        pytest.skip('本机没有可验证的隔离')
    secret = tmp_path / 'host-secret.txt'
    secret.write_text('绝密')
    v, files = version(f"""
        import json, sys
        json.load(sys.stdin)
        try:
            text = open({str(secret)!r}, encoding='utf-8').read()
            print(json.dumps({{'status': 'ok', 'result': {{'read': text}}}}))
        except Exception as exc:
            print(json.dumps({{'status': 'ok', 'result': {{'read': type(exc).__name__}}}}))
    """)
    outcome = run_tool(v, files, [])
    assert outcome['status'] == 'succeeded', outcome
    assert outcome['result']['read'] != '绝密', '沙箱里读到了宿主文件'
    assert outcome['result']['read'].endswith('Error')


def test_the_tool_has_no_network_and_no_inherited_credentials():
    if not pack_sandbox.probe().available:
        pytest.skip('本机没有可验证的隔离')
    v, files = version("""
        import json, os, socket, sys
        json.load(sys.stdin)
        try:
            socket.create_connection(('1.1.1.1', 53), timeout=3).close(); net = 'connected'
        except Exception as exc:
            net = type(exc).__name__
        leaked = sorted(k for k in os.environ if 'KEY' in k or 'TOKEN' in k or 'SECRET' in k)
        print(json.dumps({'status': 'ok', 'result': {'net': net, 'leaked': leaked}}))
    """)
    outcome = run_tool(v, files, [])
    assert outcome['status'] == 'succeeded', outcome
    assert outcome['result']['net'] != 'connected'
    assert outcome['result']['leaked'] == []


# ---- P1-5 schema 真的被执行 -------------------------------------------------
def test_output_schema_enforces_enum_and_nested_constraints():
    problems = validate_instance({'status': 'invented'}, BASE_SCHEMA_OUT, where='输出')
    assert len(problems) == 1 and 'is not one of' in problems[0]
    assert validate_instance({'status': 'ok'}, BASE_SCHEMA_OUT, where='输出') == []
    nested = {'type': 'object', 'properties': {'result': {'type': 'object',
              'properties': {'total': {'type': 'number'}}}}}
    assert validate_instance({'result': {'total': '不是数字'}}, nested, where='输出')


def test_remote_refs_are_refused():
    remote = {'type': 'object', 'properties': {'a': {'$ref': 'https://example.com/s.json'}}}
    problems = validate_instance({'a': 1}, remote, where='输出')
    assert len(problems) == 1 and '远端解析' in problems[0] and '$ref' in problems[0]


def test_input_schema_is_actually_executed():
    strict = {'type': 'object', 'required': ['must_be_there'], 'properties': {'must_be_there': {'type': 'string'}}}
    v, files = version("""
        import json, sys
        json.load(sys.stdin)
        print(json.dumps({'status': 'ok', 'result': {'a': 1}}))
    """, input_schema=strict)
    outcome = run_tool(v, files, [])
    assert outcome['status'] == 'failed' and outcome['error_code'] == 'bad_input'
    assert 'must_be_there' in outcome['error']


def test_a_tool_declaring_a_bad_output_status_is_rejected():
    v, files = version("""
        import json, sys
        json.load(sys.stdin)
        print(json.dumps({'status': 'definitely-fine', 'result': {'a': 1}}))
    """)
    outcome = run_tool(v, files, [])
    assert outcome['status'] == 'failed'
    assert outcome['validation_status'] == 'failed'


@pytest.mark.parametrize('declared', ['"not-a-list"', '[1, 2, 3]', '[{"path": 5}]', '[{}]'])
def test_malformed_outputs_become_structured_problems_not_exceptions(declared):
    v, files = version(f"""
        import json, sys
        json.load(sys.stdin)
        print(json.dumps({{'status': 'ok', 'outputs': {declared}, 'result': {{'a': 1}}}}))
    """)
    outcome = run_tool(v, files, [])
    assert outcome['status'] == 'failed'
    assert isinstance(outcome['error'], str) and outcome['error']
    assert outcome['outputs'] == []


# ---- P1-6 超时、进程组回收、取消、输出限额 ----------------------------------
def test_timeout_reclaims_the_whole_process_group(tmp_path):
    if not pack_sandbox.probe().available:
        pytest.skip('本机没有可验证的隔离')
    workdir = tmp_path / 'work'
    orphan = 'output/orphan.txt'
    v, files = version(f"""
        import json, subprocess, sys, time
        json.load(sys.stdin)
        subprocess.Popen([sys.executable, '-I', '-c',
            "import time; time.sleep(6); open({orphan!r}, 'w').write('survived')"])
        time.sleep(30)
    """, timeout=2)
    started = time.time()
    outcome = run_tool(v, files, [], workdir=workdir)
    assert outcome['error_code'] == 'timeout'
    assert time.time() - started < 20
    time.sleep(8)  # 孤儿本来会在第 6 秒写文件
    assert not (workdir / orphan).exists(), '超时只杀了直接子进程，孙子进程活了下来'


def test_cancel_kills_a_running_tool(tmp_path):
    if not pack_sandbox.probe().available:
        pytest.skip('本机没有可验证的隔离')
    workdir = tmp_path / 'work'
    v, files = version("""
        import json, sys, time
        json.load(sys.stdin)
        time.sleep(60)
        print(json.dumps({'status': 'ok'}))
    """, timeout=120)
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    started = time.time()
    outcome = run_tool(v, files, [], workdir=workdir, cancel=cancel)
    assert outcome['status'] == 'cancelled' and outcome['error_code'] == 'cancelled'
    assert time.time() - started < 30, '取消没有真的中止工具进程'


def test_unbounded_stdout_is_capped_while_streaming():
    v, files = version("""
        import json, sys
        json.load(sys.stdin)
        block = 'x' * 65536
        for _ in range(200):
            sys.stdout.write(block)
        sys.stdout.flush()
    """, timeout=30)
    started = time.time()
    outcome = run_tool(v, files, [])
    assert outcome['error_code'] == 'output_too_large'
    assert time.time() - started < 25


# ---- 验证用例的期望字段 ------------------------------------------------------
def test_unknown_expectation_fields_never_count_as_a_pass():
    v, files = version("""
        import json, sys
        json.load(sys.stdin)
        print(json.dumps({'status': 'ok', 'result': {'a': 1}}))
    """)
    files = {**files, 'fixtures/tests.json': json.dumps(
        {'cases': [{'id': '写错了期望字段', 'inputs': [], 'expect': {'resultz': {'a': 1}}}]}).encode()}
    report = evaluate(v, files)
    assert report['passed'] is False
    assert '不认识的期望字段' in report['cases'][0]['reason']

# ---- Linux 后端：命令行构造（本机跑不了 bwrap，至少把形状钉死）--------------
def test_bwrap_argv_isolates_the_network_and_mounts_nothing_from_the_host(monkeypatch, tmp_path):
    monkeypatch.setattr(pack_sandbox, 'backend_name', lambda: 'bwrap')
    base, out, tmp = tmp_path / 'base', tmp_path / 'base/output', tmp_path / 'base/tmp'
    for folder in (base, out, tmp):
        folder.mkdir(parents=True, exist_ok=True)
    argv = pack_sandbox.build_argv(['python3', 'x.py'], base=base, output_dir=out, tmp_dir=tmp)
    assert argv[0] == 'bwrap'
    # 网络靠命名空间隔离，不是靠策略语句。
    assert '--unshare-all' in argv and '--die-with-parent' in argv
    # 不整树 ro-bind 根：/home、/root、/var、/opt、/etc 在沙箱里根本不存在。
    pairs = list(zip(argv, argv[1:]))
    assert ('--ro-bind', '/') not in pairs
    assert not any(flag.startswith('--') and target in ('/home', '/root', '/var', '/opt')
                   for flag, target in pairs)
    # 唯一的可写出口是 output 与私有 tmp；工作目录只读。
    writable = [target for flag, target in pairs if flag == '--bind']
    assert str(out.resolve()) in writable and str(tmp.resolve()) in writable
    assert str(base.resolve()) in [target for flag, target in pairs if flag == '--ro-bind']
    assert str(base.resolve()) not in writable


def test_an_unsupported_platform_refuses_instead_of_running_bare(monkeypatch, tmp_path):
    monkeypatch.setattr(pack_sandbox, 'backend_name', lambda: None)
    with pytest.raises(pack_sandbox.IsolationUnavailable):
        pack_sandbox.build_argv(['python3'], base=tmp_path, output_dir=tmp_path, tmp_dir=tmp_path)


def test_sandbox_environment_carries_no_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'must-not-leak')
    monkeypatch.setenv('GITHUB_TOKEN', 'must-not-leak')
    env = pack_sandbox.sandbox_env(tmp_path)
    assert 'must-not-leak' not in ''.join(env.values())
    assert set(env) == {'PATH', 'HOME', 'TMPDIR', 'LC_ALL', 'PYTHONIOENCODING', 'PYTHONDONTWRITEBYTECODE'}

# ---- 输入/输出总量与临时目录 ------------------------------------------------
def test_input_over_the_declared_limit_is_refused_before_execution(tmp_path):
    v, files = version("""
        import json, sys
        json.load(sys.stdin)
        print(json.dumps({'status': 'ok', 'result': {'a': 1}}))
    """)
    v['manifest']['tool']['permissions']['max_input_bytes'] = 1024
    outcome = run_tool(v, files, [{'name': 'big.bin', 'content': b'x' * 4096}])
    assert outcome['status'] == 'failed' and outcome['error_code'] == 'input_too_large'


def test_too_many_output_files_are_capped_with_a_reason():
    v, files = version(f"""
        import json, sys
        from pathlib import Path
        json.load(sys.stdin)
        names = []
        for i in range({pack_runtime.MAX_OUTPUT_FILES + 5}):
            name = f'out-{{i}}.txt'
            Path('output') .joinpath(name).write_text('x')
            names.append({{'path': name}})
        print(json.dumps({{'status': 'ok', 'outputs': names}}))
    """)
    if not pack_sandbox.probe().available:
        pytest.skip('本机没有可验证的隔离')
    outcome = run_tool(v, files, [])
    assert outcome['status'] == 'failed'
    assert '输出文件数量超过' in outcome['error']
    assert len(outcome['outputs']) <= pack_runtime.MAX_OUTPUT_FILES


def test_the_working_directory_is_removed_after_a_run(monkeypatch):
    if not pack_sandbox.probe().available:
        pytest.skip('本机没有可验证的隔离')
    created = []
    real = pack_runtime.tempfile.mkdtemp

    def spy(*args, **kwargs):
        path = real(*args, **kwargs)
        if kwargs.get('prefix') == 'pack-task-':
            created.append(path)
        return path

    monkeypatch.setattr(pack_runtime.tempfile, 'mkdtemp', spy)
    v, files = version("""
        import json, sys
        from pathlib import Path
        json.load(sys.stdin)
        Path('output/a.txt').write_text('x')
        print(json.dumps({'status': 'ok', 'outputs': [{'path': 'a.txt'}]}))
    """)
    outcome = run_tool(v, files, [])
    assert outcome['status'] == 'succeeded'
    assert created and not Path(created[0]).exists(), '工作目录没有清理'

# ---- 复核轮次 2：运行时根目录、非阻塞读、进程组 ------------------------------
def test_runtime_roots_keep_the_standard_library_for_a_venv_on_a_conda_base(monkeypatch):
    """Conda base 上建的 venv：sys.prefix 是 .venv，标准库却在 base_prefix 下。

    早先只取「排序后的首末两条」，正好把标准库根丢掉，探针报
    Could not find platform independent/dependent libraries。
    """
    monkeypatch.setattr(pack_sandbox.sys, 'prefix', '/tmp/demo-venv')
    monkeypatch.setattr(pack_sandbox.sys, 'exec_prefix', '/tmp/demo-venv')
    monkeypatch.setattr(pack_sandbox.sys, 'base_prefix', '/opt/base-python')
    monkeypatch.setattr(pack_sandbox.sys, 'base_exec_prefix', '/opt/base-python')
    monkeypatch.setattr(pack_sandbox.sys, 'executable', '/tmp/demo-venv/bin/python')
    monkeypatch.setattr(pack_sandbox.sysconfig, 'get_paths', lambda: {
        'stdlib': '/opt/base-python/lib/python3.12',
        'platstdlib': '/opt/base-python/lib/python3.12',
        'purelib': '/tmp/demo-venv/lib/python3.12/site-packages',
        'platlib': '/tmp/demo-venv/lib/python3.12/site-packages',
        'scripts': '/tmp/demo-venv/bin', 'data': '/tmp/demo-venv'})
    roots = [str(item) for item in pack_sandbox.runtime_roots()]
    assert '/opt/base-python' in roots, f'标准库根丢了：{roots}'
    assert any(item.rstrip('/').endswith('demo-venv') for item in roots), roots
    # 收敛到祖先，不会把每个子目录都写成一条规则，更不会放开整个 /opt。
    assert '/opt/base-python/lib/python3.12' not in roots
    assert '/opt' not in roots and '/' not in roots


def test_every_runtime_root_reaches_the_seatbelt_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(pack_sandbox, 'backend_name', lambda: 'seatbelt')
    monkeypatch.setattr(pack_sandbox, 'runtime_roots',
                        lambda: [Path('/opt/base-python'), Path('/opt/venv-one'), Path('/opt/venv-two')])
    base = tmp_path / 'base'
    out, tmp = base / 'output', base / 'tmp'
    for folder in (base, out, tmp):
        folder.mkdir(parents=True, exist_ok=True)
    argv = pack_sandbox.build_argv(['/opt/venv-one/bin/python'], base=base, output_dir=out, tmp_dir=tmp)
    params = dict(zip(argv, argv[1:]))
    handed = {value.split('=', 1)[1] for key, value in zip(argv, argv[1:])
              if key == '-D' and value.startswith('PY')}
    assert handed == {'/opt/base-python', '/opt/venv-one', '/opt/venv-two'}
    profile = Path(params['-f']).read_text(encoding='utf-8')
    for index in range(3):
        assert f'(param "PY{index}")' in profile
    assert '(deny default)' in profile and '(deny network*)' in profile
    del params


def test_symlinked_ancestors_of_the_interpreter_are_allowed_but_nothing_else(tmp_path):
    link = tmp_path / 'linked'
    link.symlink_to(tmp_path / 'real', target_is_directory=True)
    (tmp_path / 'real').mkdir()
    found = [str(item) for item in pack_sandbox.symlink_ancestors([link / 'bin' / 'python'])]
    assert str(link) in found
    assert str(tmp_path / 'real') not in found


def test_a_grandchild_holding_stdout_cannot_outlast_the_deadline():
    """主进程先退出、孙进程继承 stdout 并 sleep(3)。

    早先 select 未就绪时走的是阻塞 `stdout.read()`，实测 0.2 秒的期限被拖到 3.04 秒，
    还把结果报成 exited。现在全程非阻塞 + 排空窗口有界。
    """
    if not pack_sandbox.probe().available:
        pytest.skip('本机没有可验证的隔离')
    v, files = version("""
        import json, subprocess, sys
        json.load(sys.stdin)
        subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(3)'])
        print(json.dumps({'status': 'ok', 'result': {'a': 1}}))
        sys.stdout.flush()
    """, timeout=1)
    started = time.time()
    outcome = run_tool(v, files, [])
    elapsed = time.time() - started
    assert elapsed < 2.5, f'孙进程把读操作拖住了：{elapsed:.2f}s'
    # 主进程确实完成了，结果照常解析出来；孙进程由进程组回收兜底。
    assert outcome['status'] == 'succeeded' and outcome['result'] == {'a': 1}


def test_a_grandchild_holding_stdout_does_not_survive_the_run(tmp_path):
    if not pack_sandbox.probe().available:
        pytest.skip('本机没有可验证的隔离')
    workdir = tmp_path / 'work'
    v, files = version("""
        import json, subprocess, sys
        json.load(sys.stdin)
        subprocess.Popen([sys.executable, '-I', '-c',
            "import time; time.sleep(4); open('output/orphan.txt','w').write('survived')"])
        print(json.dumps({'status': 'ok', 'result': {'a': 1}}))
        sys.stdout.flush()
    """, timeout=10)
    outcome = run_tool(v, files, [], workdir=workdir)
    assert outcome['status'] == 'succeeded'
    time.sleep(6)
    assert not (workdir / 'output' / 'orphan.txt').exists(), '正常结束也必须回收进程组'


def test_a_silent_grandchild_does_not_stop_the_timeout_from_firing():
    if not pack_sandbox.probe().available:
        pytest.skip('本机没有可验证的隔离')
    v, files = version("""
        import json, subprocess, sys, time
        json.load(sys.stdin)
        subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)'])
        time.sleep(30)
    """, timeout=2)
    started = time.time()
    outcome = run_tool(v, files, [])
    assert outcome['error_code'] == 'timeout'
    assert time.time() - started < 15


# ---- 复核轮次 2：schema 引用 -------------------------------------------------
def test_a_broken_local_ref_is_a_structured_problem_not_an_exception():
    problems = validate_instance({}, {'$ref': '#/$defs/missing'}, where='输入')
    assert len(problems) == 1 and '引用无法解析' in problems[0]


def test_a_valid_local_ref_still_works():
    schema = {'type': 'object', '$defs': {'name': {'type': 'string'}},
              'properties': {'a': {'$ref': '#/$defs/name'}}}
    assert validate_instance({'a': 'ok'}, schema, where='输入') == []
    assert validate_instance({'a': 1}, schema, where='输入')


@pytest.mark.parametrize('schema', [
    {'$ref': 'https://example.com/s.json'},
    {'$dynamicRef': 'https://example.com/s.json#a'},
    {'$recursiveRef': 'https://example.com/s.json#'},
    {'$id': 'https://example.com/base', 'type': 'object'},
    {'type': 'object', 'properties': {'a': {'$dynamicRef': 'http://example.com/x#y'}}},
])
def test_any_reference_that_could_hit_the_network_is_refused(schema):
    problems = validate_instance({'a': 1}, schema, where='输入')
    assert len(problems) == 1 and '远端解析' in problems[0]


def test_a_tool_whose_contract_has_a_broken_ref_fails_the_task_with_a_reason():
    """契约写错不能让任务卡在没有终态的异常上。"""
    if not pack_sandbox.probe().available:
        pytest.skip('本机没有可验证的隔离')
    v, files = version("""
        import json, sys
        json.load(sys.stdin)
        print(json.dumps({'status': 'ok', 'result': {'a': 1}}))
    """, input_schema={'$ref': '#/$defs/nope'})
    outcome = run_tool(v, files, [])
    assert outcome['status'] == 'failed' and outcome['error_code'] == 'bad_input'
    assert '引用无法解析' in outcome['error']

REPO_ROOT = Path(__file__).resolve().parents[1]
_VENV_PROBE = ('import json, sys\n'
               'sys.path.insert(0, {root!r})\n'
               'from factory.control.pack_sandbox import probe, runtime_roots\n'
               'print(json.dumps({{"probe": probe(refresh=True).as_dict(),\n'
               '                  "roots": [str(item) for item in runtime_roots()]}}))\n')


@pytest.mark.parametrize('through_symlink', [False, True])
def test_the_canary_passes_for_an_interpreter_inside_a_venv(tmp_path, through_symlink):
    """真的建一个 venv，用它的解释器跑金丝雀。

    venv 的 `sys.prefix` 与标准库所在的 `base_prefix` 不是同一棵树，而通过符号链接
    访问时 exec 走的还是未解析的路径——这两点各让隔离探针挂过一次。
    """
    if not pack_sandbox.probe().available:
        pytest.skip('本机没有可验证的隔离')
    real = tmp_path / 'real'
    real.mkdir()
    home = tmp_path / 'linked'
    if through_symlink:
        home.symlink_to(real, target_is_directory=True)
    else:
        home = real
    venv_dir = home / 'venv'
    try:
        subprocess.run([sys.executable, '-m', 'venv', str(venv_dir)],
                       check=True, capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f'本机建不了 venv：{exc}')
    probe_source = _VENV_PROBE.format(root=str(REPO_ROOT))
    result = subprocess.run([str(venv_dir / 'bin' / 'python'), '-c', probe_source],
                            capture_output=True, timeout=300)
    assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')[-800:]
    report = json.loads(result.stdout.decode())
    assert report['probe']['available'] is True, report
    # 标准库根来自 base_prefix，必须在放开的根目录里。
    assert any(str(Path(sys.base_prefix)) == root for root in report['roots']), report['roots']
