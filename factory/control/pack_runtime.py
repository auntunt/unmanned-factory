"""职能包工具的隔离执行，以及决定候选能否发布的验证。

工具是普通程序，不是模型：固定入口、JSON 进 JSON 出、超时、资源上限、只读输入、
一个可写输出目录、无网络。工具**自称**什么从来不是结果——运行时独立检查声明的产物
是否真的存在、是否在限额内、是否符合声明的输出 schema；没有任何产出的「成功」
以 no_evidence 判失败。

三条硬约束：
  1. **没有可验证的隔离就不执行。** 不存在「记一条 sandbox: none 然后照跑」的路径。
  2. **父进程不受影响。** 能力探测一律在短命子进程里做，绝不修改服务进程的 rlimit。
  3. **不执行清单里的任何字符串。** 依赖只做声明解析与已安装元数据比对，不 import。
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import resource
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from importlib import metadata
from pathlib import Path

from factory.control.pack_sandbox import IsolationUnavailable, build_argv, probe, sandbox_env

MAX_STDOUT = 256 * 1024
MAX_STDERR = 16 * 1024
MEMORY_LIMIT = 1024 * 1024 * 1024
MAX_TOTAL_OUTPUT = 64 * 1024 * 1024
MAX_OUTPUT_FILES = 64
POLL_INTERVAL_S = 0.05
# 主进程退出后给 stdout 的排空窗口。够短，孙进程持有 stdout 时也不会拖住截止时间。
STDOUT_DRAIN_GRACE_S = 0.5

_MEMORY_PROBE = (
    'import resource,sys\n'
    'limit = int(sys.argv[1])\n'
    'for name in ("RLIMIT_AS", "RLIMIT_DATA"):\n'
    '    which = getattr(resource, name, None)\n'
    '    if which is None: continue\n'
    '    try:\n'
    '        resource.setrlimit(which, (limit, limit))\n'
    '    except (ValueError, OSError):\n'
    '        continue\n'
    '    raise SystemExit(0)\n'
    'raise SystemExit(1)\n'
)

_memory_supported: bool | None = None


def memory_limit_supported() -> bool:
    """本机能不能给子进程设内存上限。

    **在短命子进程里探测**，父进程的 rlimit 一个字节都不动。早先版本在模块导入时于
    服务进程里先降后升：macOS 直接拒绝下调（"current limit exceeds maximum limit"），
    Linux 上降下去之后恢复会抛 ValueError: not allowed to raise maximum limit——
    等于服务进程被永久降级。探测结果只取决于本机，按进程缓存一次。
    """
    global _memory_supported
    if _memory_supported is None:
        try:
            proc = subprocess.run([sys.executable, '-I', '-c', _MEMORY_PROBE, str(MEMORY_LIMIT)],
                                  capture_output=True, timeout=30, env={'PATH': '/usr/bin:/bin'})
            _memory_supported = proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _memory_supported = False
    return _memory_supported


def _limits(timeout: int, max_output: int, memory: bool):
    def apply():
        resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_output, max_output))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        if memory:
            for name in ('RLIMIT_AS', 'RLIMIT_DATA'):
                which = getattr(resource, name, None)
                if which is None:
                    continue
                try:
                    resource.setrlimit(which, (MEMORY_LIMIT, MEMORY_LIMIT))
                    break
                except (ValueError, OSError):
                    continue
    return apply


# ---- 依赖声明：解析，不执行 ------------------------------------------------
def _requirement(text):
    from packaging.requirements import InvalidRequirement, Requirement
    try:
        return Requirement(str(text))
    except InvalidRequirement as exc:
        raise ValueError(f'依赖声明不合法：{text!r}（{exc}）') from None


def check_dependencies(packages):
    """按标准依赖声明（PEP 508）核对**已安装的元数据**。

    绝不把清单里的字符串拼进 `python -c` 再执行——那等于让包作者在未隔离的探测里
    任意执行代码（Codex 的反例实测在 /tmp 落下了标记文件）。这里只做两件事：
    解析声明，然后查 importlib.metadata 里已安装的版本。不 import 被声明的包。
    """
    missing, problems = [], []
    for raw in packages:
        try:
            requirement = _requirement(raw)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue  # 环境标记不适用于本机，不算缺失
        try:
            installed = metadata.version(requirement.name)
        except metadata.PackageNotFoundError:
            missing.append(str(requirement))
            continue
        if requirement.specifier and not requirement.specifier.contains(installed, prereleases=True):
            missing.append(f'{requirement}（已安装 {installed}）')
    return missing, problems


def _python_ok(constraint):
    """Python 版本约束按完整版本比对，不是只比主版本。

    裸写 "3" 视为 ">=3"；写成 "3.12" 视为 "==3.12.*"；也接受完整的 specifier（如 ">=3.12,<4"）。
    """
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import Version
    text = str(constraint or '').strip()
    if not text:
        return True, ''
    current = Version(platform.python_version())
    if text[0].isdigit():
        parts = text.split('.')
        text = f'>={text}' if len(parts) == 1 else f'=={text}.*'
    try:
        return SpecifierSet(text).contains(current, prereleases=True), text
    except InvalidSpecifier:
        return False, text


def environment_report(manifest) -> dict:
    """这个版本在**本机**能不能跑。与「已发布」「有没有权限」是三件独立的事。

    隔离不可用时环境就是 unavailable——因为在这套约定里，没有隔离就不执行。
    """
    lock = manifest['dependency_lock']
    missing, problems = check_dependencies(lock.get('packages', []))
    ok, normalized = _python_ok(lock.get('python'))
    if not ok:
        missing.append(f'python {normalized}（本机 {platform.python_version()}）')
    isolation = probe()
    if not isolation.available:
        problems.append(f'隔离不可用：{isolation.reason}')
    return {'status': 'ready' if not missing and not problems else 'unavailable',
            'missing': missing, 'problems': problems,
            'python': platform.python_version(), 'platform': platform.platform(),
            'sandbox': isolation.backend or 'none', 'isolation_verified': isolation.available,
            'memory_limit': MEMORY_LIMIT if memory_limit_supported() else 'unsupported_on_host'}


# ---- schema ---------------------------------------------------------------
def _validator(schema, *, where):
    """用 jsonschema 自检 schema 再校验实例；远端 $ref 一律拒绝。

    早先那个手写的 `_schema_check` 只看 required 与顶层 type，enum、嵌套、
    additionalProperties 全部忽略——注释说「未知构造会被报告」，实际是静默放行。
    """
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    if not isinstance(schema, dict):
        raise ValueError(f'{where} schema 必须是对象')
    remote = _remote_reference(schema)
    if remote:
        raise ValueError(f'{where} 含会触发远端解析的引用（{remote}），出于安全不允许')
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f'{where} 不是合法的 JSON Schema：{exc.message}') from None
    return Draft202012Validator(schema)


# 任何可能触发远端解析的引用关键字都不许出现。只挡 `$ref` 不够：`$dynamicRef` /
# `$recursiveRef` 同样会去解析，`$id` / `$schema` 指向 http 会把基准 URI 变成远端。
_REF_KEYS = ('$ref', '$dynamicRef', '$recursiveRef')
_BASE_KEYS = ('$id', '$schema')


def _remote_reference(node, path='(根)'):
    """返回第一处会触发远端解析的引用位置；没有则返回 None。"""
    if isinstance(node, dict):
        for key in _REF_KEYS:
            value = node.get(key)
            if isinstance(value, str) and not value.startswith('#'):
                return f'{path} 的 {key}'
        for key in _BASE_KEYS:
            value = node.get(key)
            if isinstance(value, str) and '://' in value:
                return f'{path} 的 {key}'
        for key, value in node.items():
            found = _remote_reference(value, f'{path}/{key}')
            if found:
                return found
        return None
    if isinstance(node, list):
        for index, value in enumerate(node):
            found = _remote_reference(value, f'{path}[{index}]')
            if found:
                return found
    return None


def _schema_check(instance, schema):
    """兼容旧签名的薄封装：返回问题列表（空列表表示通过）。

    旧实现只看顶层 type 与 required，enum / 嵌套 / additionalProperties 全部静默放行。
    现在一律交给 jsonschema。"""
    return validate_instance(instance, schema, where='输出')


def validate_instance(instance, schema, *, where):
    """返回结构化的问题列表（路径 + 说明），**任何情况下都不抛异常给调用方**。

    指向不存在锚点的本地 `$ref`（`#/$defs/missing`）能通过 check_schema，却在校验时抛
    `PointerToNowhere`——那会让一次调用变成没有终态的异常，而不是一条「契约写错了」。
    引用解析失败在这里同样变成结构化问题。
    """
    try:
        validator = _validator(schema, where=where)
    except ValueError as exc:
        return [str(exc)]
    try:
        errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.absolute_path))
    except Exception as exc:  # 引用解析失败等：转成结构化问题，不让它冒泡卡住任务
        return [f'{where} schema 引用无法解析：{type(exc).__name__}: {str(exc)[:200]}']
    problems = []
    for error in errors:
        location = '/'.join(str(part) for part in error.absolute_path) or '(根)'
        problems.append(f'{where} {location}：{error.message}')
    return problems[:20]


# ---- 执行 ------------------------------------------------------------------
def _reclaim(proc, pgid):
    """回收整个进程组，**即使组首领已经退出**。

    只 kill 直接子进程会留下孤儿继续跑（实测过）。pgid 在 spawn 之后立刻记下来：
    等 proc 被 wait 掉之后再去 os.getpgid(pid) 会抛 ProcessLookupError，于是「清理」
    静默变成没清理。start_new_session=True 保证 pgid == 子进程 pid。
    """
    if pgid:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.SubprocessError:
        pass


def _stream(proc, *, deadline, cancel, pgid):
    """**全程非阻塞、全程有截止时间**地读 stdout。

    先收进内存再判大小，一个狂写 stdout 的工具就能把服务打爆；而主进程退出后直接
    `stdout.read()` 会阻塞——孙进程继承了同一个 stdout，它不退出这里就一直等
    （实测 deadline 0.2s、孙进程 sleep(3)，实际 3.04s 才返回，还误报 exited）。
    所以：主进程退出后只给一个很短的排空窗口，窗口内仍然只走 select；窗口过了就收工，
    截止时间任何一轮都优先于排空。

    返回 (stdout, 结束原因)。原因 ∈ {'exited','timeout','cancelled','stdout_too_large'}。
    """
    handle = proc.stdout
    fileno = handle.fileno()
    os.set_blocking(fileno, False)
    chunks, total, drain_until = [], 0, None
    while True:
        if cancel is not None and cancel.is_set():
            return b''.join(chunks), 'cancelled'
        if drain_until is not None and time.monotonic() > drain_until:
            # 主进程已退出，stdout 仍被别人持有：不等了，交给调用方回收进程组。
            return b''.join(chunks), 'exited'
        if time.monotonic() > deadline:
            return b''.join(chunks), 'timeout'
        ready, _, _ = select.select([fileno], [], [], POLL_INTERVAL_S)
        if ready:
            try:
                data = os.read(fileno, 65536)
            except BlockingIOError:
                data = None
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                return b''.join(chunks), 'exited'
            if data == b'':
                return b''.join(chunks), 'exited'  # 真正的 EOF：所有写端都关了
            if data:
                total += len(data)
                if total > MAX_STDOUT:
                    return b''.join(chunks), 'stdout_too_large'
                chunks.append(data)
                continue
        if proc.poll() is not None and drain_until is None:
            drain_until = min(time.monotonic() + STDOUT_DRAIN_GRACE_S, deadline + STDOUT_DRAIN_GRACE_S)


def _failure(code, message, *, evidence, started, outputs=()):
    return {'status': 'failed', 'error_code': code, 'validation_status': 'failed',
            'error': message, 'outputs': list(outputs), 'evidence': evidence,
            'duration_ms': int((time.time() - started) * 1000)}


def run_tool(version, files: dict[str, bytes], inputs, *, options=None, workdir=None, cancel=None):
    """执行一次调用。任何结局——拒绝、超时、崩溃、校验不过——都返回结构化结果。"""
    manifest = version['manifest']
    tool = manifest['tool']
    timeout = int(tool['timeout_seconds'])
    started = time.time()
    isolation = probe()
    evidence = {'isolation': isolation.backend or 'none', 'isolation_verified': isolation.available,
                'network': 'denied' if isolation.available else 'not_enforced',
                'timeout_seconds': timeout, 'python': platform.python_version(),
                'limits': {'cpu_seconds': timeout,
                           'max_output_bytes': int(tool['permissions']['max_output_bytes']),
                           'memory_bytes': MEMORY_LIMIT if memory_limit_supported() else 'unsupported_on_host'}}
    if not isolation.available:
        # 没有可验证的隔离就不执行。记一条日志然后照跑，等于把「隔离」变成文档里的一句话。
        return _failure('isolation_unavailable', f'本机没有可验证的隔离，拒绝执行工具：{isolation.reason}',
                        evidence=evidence, started=started)

    owned = workdir is None
    base = Path(workdir or tempfile.mkdtemp(prefix='pack-task-')).resolve()
    try:
        program, in_dir, out_dir, tmp_dir = base / 'program', base / 'input', base / 'output', base / 'tmp'
        for folder in (program, in_dir, out_dir, tmp_dir):
            folder.mkdir(parents=True, exist_ok=True)
        for path, content in files.items():
            target = program / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        declared, total_input = [], 0
        for item in inputs:
            name = Path(item['name']).name or 'input.bin'
            total_input += len(item['content'])
            if total_input > int(tool['permissions']['max_input_bytes']):
                return _failure('input_too_large', '输入超过本能力声明的上限',
                                evidence=evidence, started=started)
            (in_dir / name).write_bytes(item['content'])
            declared.append({'name': name, 'path': f'input/{name}', 'sha256': item.get('sha256')})
        request = {'input_dir': 'input', 'output_dir': 'output', 'inputs': declared,
                   'options': dict(options or {})}
        input_problems = validate_instance(request, tool['input_schema'], where='输入')
        if input_problems:
            return _failure('bad_input', '；'.join(input_problems), evidence=evidence, started=started)

        argv = [sys.executable, '-I', str(program / tool['entrypoint'])]
        try:
            wrapped = build_argv(argv, base=base, output_dir=out_dir, tmp_dir=tmp_dir)
        except IsolationUnavailable as exc:
            return _failure('isolation_unavailable', str(exc), evidence=evidence, started=started)
        stderr_path = base / 'stderr.log'
        proc = pgid = None
        try:
            with stderr_path.open('wb') as stderr_file:
                proc = subprocess.Popen(
                    wrapped, cwd=base, env=sandbox_env(tmp_dir), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=stderr_file, start_new_session=True,
                    preexec_fn=_limits(timeout, int(tool['permissions']['max_output_bytes']),
                                       memory_limit_supported()))
                # start_new_session=True → pgid == pid。**现在**记下来：等进程被 wait 掉
                # 之后再查 pgid 会失败，清理就变成静默没清理。
                pgid = proc.pid
                try:
                    proc.stdin.write(json.dumps(request).encode())
                except OSError:
                    pass
                finally:
                    proc.stdin.close()
                stdout, reason = _stream(proc, deadline=time.monotonic() + timeout,
                                         cancel=cancel, pgid=pgid)
        except OSError as exc:
            return _failure('spawn_failed', f'无法启动工具进程：{exc}', evidence=evidence, started=started)
        finally:
            # 无论哪种结局都回收整个进程组：正常结束的工具也可能留下后台孙进程。
            if proc is not None:
                _reclaim(proc, pgid)
        stderr = stderr_path.read_bytes()[:MAX_STDERR].decode('utf-8', 'replace') if stderr_path.exists() else ''

        if reason == 'timeout':
            return _failure('timeout', f'工具执行超过 {timeout} 秒，已回收整个进程组',
                            evidence=evidence, started=started)
        if reason == 'cancelled':
            return {'status': 'cancelled', 'error_code': 'cancelled', 'validation_status': 'unverified',
                    'error': '调用已取消，工具进程组已回收', 'outputs': [], 'evidence': evidence,
                    'duration_ms': int((time.time() - started) * 1000)}
        if reason == 'stdout_too_large':
            return _failure('output_too_large', '工具输出超过 256 KB，已中止',
                            evidence=evidence, started=started)
        if proc.returncode != 0:
            return _failure('tool_error', f'工具以退出码 {proc.returncode} 结束：{stderr.strip()[:800]}',
                            evidence=evidence, started=started)
        try:
            reported = json.loads(stdout.decode('utf-8'))
            if not isinstance(reported, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return _failure('bad_contract', '工具没有按契约输出 JSON 结果', evidence=evidence, started=started)

        problems = validate_instance(reported, tool['output_schema'], where='输出')
        outputs, limit, total = [], int(tool['permissions']['max_output_bytes']), 0
        declared_outputs = reported.get('outputs')
        if declared_outputs is not None and not isinstance(declared_outputs, list):
            problems.append('outputs 必须是数组')
            declared_outputs = []
        for item in (declared_outputs or [])[:MAX_OUTPUT_FILES + 1]:
            # 畸形的 outputs 元素只能变成一条问题，不能抛异常把任务卡死。
            if not isinstance(item, dict):
                problems.append(f'outputs 元素不是对象：{item!r:.60}')
                continue
            rel = str(item.get('path', ''))
            if len(outputs) >= MAX_OUTPUT_FILES:
                problems.append(f'输出文件数量超过 {MAX_OUTPUT_FILES} 个')
                break
            try:
                target = (out_dir / rel).resolve()
                inside = target.is_relative_to(out_dir.resolve())
            except (OSError, ValueError):
                problems.append(f'声明的输出路径不合法：{rel}')
                continue
            if not rel or not inside or target.is_symlink() or not target.is_file():
                problems.append(f'声明的输出文件不存在或越界：{rel}')
                continue
            size = target.stat().st_size
            total += size
            if size > limit or total > MAX_TOTAL_OUTPUT:
                problems.append(f'输出超过上限：{rel}')
                continue
            outputs.append({'name': Path(rel).name, 'path': rel, 'size': size,
                            'content': target.read_bytes(), 'kind': str(item.get('kind') or 'file'),
                            'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
        if reported.get('status') == 'ok' and not outputs and not reported.get('result'):
            problems.append('工具声称成功但没有产出任何结果文件或结构化结果')
        failed = reported.get('status') != 'ok' or problems
        return {'status': 'failed' if failed else 'succeeded',
                'error_code': (reported.get('error_code') if reported.get('status') != 'ok' else None)
                              or ('validation_failed' if problems else None),
                'validation_status': 'failed' if problems else ('passed' if not failed else 'unverified'),
                'error': '；'.join(problems) or (str(reported.get('error') or '') if failed else None) or None,
                'result': reported.get('result'), 'diagnostics': reported.get('diagnostics', []),
                'outputs': outputs, 'evidence': evidence,
                'duration_ms': int((time.time() - started) * 1000)}
    finally:
        if owned:
            shutil.rmtree(base, ignore_errors=True)


# ---- 验证 ------------------------------------------------------------------
EXPECT_KEYS = {'equals', 'sha256', 'result', 'error_code'}


def evaluate(candidate, files: dict[str, bytes], *, keep=False, cancel=None):
    """用包内测试集对**真实候选内容**逐例执行，证据绑定内容摘要。"""
    manifest = candidate['manifest']
    test_path = manifest['evaluation_policy']['test_set']
    environment = environment_report(manifest)
    try:
        spec = json.loads(files[test_path].decode('utf-8'))
        cases = spec['cases']
        if not isinstance(cases, list) or not cases:
            raise ValueError
    except (KeyError, ValueError, UnicodeDecodeError):
        return {'passed': False, 'summary': f'测试集 {test_path} 不是含 cases 数组的 JSON', 'cases': [],
                'environment': environment, 'test_set': test_path, 'scope': []}
    results = []
    base = Path(tempfile.mkdtemp(prefix='pack-eval-')).resolve()
    try:
        for index, case in enumerate(cases):
            cid = str(case.get('id') or f'case-{index + 1}')
            if cancel is not None and cancel.is_set():
                results.append({'id': cid, 'passed': False, 'reason': '验证已取消'})
                break
            unknown = sorted(set(case.get('expect') or {}) - EXPECT_KEYS)
            if unknown:
                results.append({'id': cid, 'passed': False,
                                'reason': f'用例声明了本运行时不认识的期望字段：{"、".join(unknown)}'})
                continue
            missing = [path for path in case.get('inputs', []) if path not in files]
            if missing:
                results.append({'id': cid, 'passed': False, 'reason': f'测试输入不在包内：{"、".join(missing)}'})
                continue
            inputs = [{'name': Path(path).name, 'content': files[path]} for path in case.get('inputs', [])]
            outcome = run_tool(candidate, files, inputs, options=case.get('options'),
                               workdir=base / f'case-{index + 1}', cancel=cancel)
            results.append({'id': cid, **_judge(case, outcome, files)})
        passed = bool(results) and all(result['passed'] for result in results)
        scope = [row for row in manifest['support_matrix'] if row['status'] != 'unsupported']
        return {'passed': passed, 'cases': results, 'environment': environment,
                'test_set': test_path, 'scope': scope,
                'summary': f"{sum(result['passed'] for result in results)}/{len(results)} 个用例通过"
                           + ('' if passed else '；验证未通过，不可发布')}
    finally:
        if not keep:
            shutil.rmtree(base, ignore_errors=True)


def _judge(case, outcome, files):
    expect = case.get('expect') or {}
    detail = {'status': outcome['status'], 'error_code': outcome.get('error_code'),
              'error': outcome.get('error'), 'duration_ms': outcome.get('duration_ms'),
              'isolation': outcome['evidence']['isolation']}
    if not expect:
        return {'passed': False, 'reason': '用例没有声明任何期望，不能算通过', **detail}
    if expect.get('error_code'):
        ok = outcome['status'] == 'failed' and outcome.get('error_code') == expect['error_code']
        return {'passed': ok, 'reason': '' if ok else
                f"期望失败码 {expect['error_code']}，实际 {outcome.get('error_code')}", **detail}
    if outcome['status'] != 'succeeded':
        return {'passed': False, 'reason': outcome.get('error') or '执行失败', **detail}
    produced = {item['path']: item['content'] for item in outcome['outputs']}
    for path, expected_path in (expect.get('equals') or {}).items():
        if path not in produced:
            return {'passed': False, 'reason': f'缺少输出 {path}', **detail}
        if expected_path not in files:
            return {'passed': False, 'reason': f'期望文件不在包内：{expected_path}', **detail}
        if produced[path] != files[expected_path]:
            return {'passed': False, 'reason': f'输出 {path} 与期望文件不一致', **detail}
    for path, digest in (expect.get('sha256') or {}).items():
        if path not in produced or hashlib.sha256(produced[path]).hexdigest() != digest:
            return {'passed': False, 'reason': f'输出 {path} 的校验值不匹配', **detail}
    for key, value in (expect.get('result') or {}).items():
        actual = (outcome.get('result') or {}).get(key)
        if actual != value:
            return {'passed': False, 'reason': f'结果字段 {key} 期望 {value}，实际 {actual}', **detail}
    return {'passed': True, 'reason': '', **detail}
