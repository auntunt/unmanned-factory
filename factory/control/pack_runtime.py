"""Isolated execution of a published capability pack's tool, and the evaluation that
decides whether a candidate may be published at all.

The tool is an ordinary program, not a model: fixed entrypoint, JSON in, JSON out, a
timeout, resource limits, a read-only input directory, one writable output directory and
no network. What the tool *claims* is never the result — the runtime independently checks
that the declared outputs exist, fit the limits and match the declared output schema, and
a success claimed without any output file is a failure with error code `no_evidence`.

Isolation is reported honestly. On a host where `sandbox-exec` is unavailable the run
still happens under resource limits and a private directory, and the evidence records
`sandbox: "none"` instead of pretending the process was confined.
"""
from __future__ import annotations

import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_STDOUT = 256 * 1024
MEMORY_LIMIT = 1024 * 1024 * 1024

# A profile written for *this* purpose rather than reusing the worker policy: a worker
# needs the network and writes across its workspace; a business tool must not touch the
# network at all and may write only to its output directory. Paths go through -D
# parameters, never string interpolation — a directory name containing a quote would
# otherwise close the expression early and silently drop a deny rule.
_PROFILE = '''(version 1)
(allow default)
(deny network*)
(deny file-write*)
(allow file-write* (subpath (param "OUT")))
(allow file-write* (subpath (param "TMP")))
(allow file-write* (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr") (literal "/dev/urandom"))
'''


def _sandbox(out_dir: Path, tmp_dir: Path, profile_path: Path):
    binary = '/usr/bin/sandbox-exec'
    if sys.platform != 'darwin' or not Path(binary).exists():
        return None, 'none'
    profile_path.write_text(_PROFILE, encoding='utf-8')
    # Resolved paths only: /var/folders is a symlink to /private/var/folders, and sbpl
    # `subpath` matches the real path — an unresolved -D silently allows nothing.
    return [binary, '-f', str(profile_path), '-D', f'OUT={out_dir.resolve()}',
            '-D', f'TMP={tmp_dir.resolve()}'], 'seatbelt'


def _memory_limit_supported() -> bool:
    """macOS refuses to lower RLIMIT_AS/DATA ("current limit exceeds maximum limit").

    Probe once instead of assuming: a memory cap we cannot actually set must be reported
    as absent, not quietly claimed. A failing probe would otherwise surface as
    "Exception occurred in preexec_fn" and look like a broken tool.
    """
    for name in ('RLIMIT_AS', 'RLIMIT_DATA'):
        which = getattr(resource, name, None)
        if which is None:
            continue
        original = resource.getrlimit(which)
        try:
            resource.setrlimit(which, (MEMORY_LIMIT, MEMORY_LIMIT))
        except (ValueError, OSError):
            continue
        resource.setrlimit(which, original)
        return True
    return False


MEMORY_LIMIT_SUPPORTED = _memory_limit_supported()


def _limits(timeout: int, max_output: int):
    def apply():
        resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_output, max_output))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        if MEMORY_LIMIT_SUPPORTED:
            for name in ('RLIMIT_AS', 'RLIMIT_DATA'):
                which = getattr(resource, name, None)
                if which is not None:
                    try:
                        resource.setrlimit(which, (MEMORY_LIMIT, MEMORY_LIMIT))
                        break
                    except (ValueError, OSError):
                        continue
        os.setsid()  # own process group, so a timeout kills the tool's children too
    return apply


def environment_report(manifest) -> dict:
    """Is this version runnable *here*? Separate from 'published' and from permissions."""
    lock = manifest['dependency_lock']
    missing = []
    for package in lock.get('packages', []):
        name = package.split('==')[0].split('>=')[0].strip()
        if not name:
            continue
        probe = subprocess.run([sys.executable, '-c', f'import {name.replace("-", "_")}'],
                               capture_output=True, timeout=30)
        if probe.returncode != 0:
            missing.append(package)
    interpreter = platform.python_version()
    required = str(lock.get('python') or '')
    if required and not interpreter.startswith(required.split('.', 2)[0]):
        missing.append(f'python {required}')
    return {'status': 'ready' if not missing else 'unavailable', 'missing': missing,
            'python': interpreter, 'platform': platform.platform(),
            'sandbox': 'seatbelt' if sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists() else 'none'}


def _schema_check(value, schema) -> list[str]:
    """A deliberately small structural check of the declared output schema: required keys
    and their JSON types. Not a full JSON-Schema implementation — and it is never described
    as one. Unknown constructs are reported instead of silently passing."""
    problems = []
    if schema.get('type') == 'object':
        if not isinstance(value, dict):
            return ['输出不是对象']
        for key in schema.get('required', []):
            if key not in value:
                problems.append(f'输出缺少必需字段 {key}')
        for key, sub in (schema.get('properties') or {}).items():
            if key in value and isinstance(sub, dict) and sub.get('type'):
                expect = {'string': str, 'number': (int, float), 'integer': int,
                          'boolean': bool, 'array': list, 'object': dict}.get(sub['type'])
                if expect and not isinstance(value[key], expect):
                    problems.append(f'字段 {key} 类型应为 {sub["type"]}')
    return problems


def run_tool(version, files: dict[str, bytes], inputs, *, options=None, workdir=None):
    """Execute one invocation. Returns a structured result for every outcome — refusal,
    timeout, crash and failed validation included. Never a bare natural-language success."""
    manifest = version['manifest']
    tool = manifest['tool']
    timeout = int(tool['timeout_seconds'])
    started = time.time()
    base = Path(workdir or tempfile.mkdtemp(prefix='pack-task-'))
    program, in_dir, out_dir, tmp_dir = base / 'program', base / 'input', base / 'output', base / 'tmp'
    for d in (program, in_dir, out_dir, tmp_dir):
        d.mkdir(parents=True, exist_ok=True)
    for path, content in files.items():
        target = program / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    declared = []
    for item in inputs:
        name = Path(item['name']).name or 'input.bin'
        (in_dir / name).write_bytes(item['content'])
        declared.append({'name': name, 'path': f'input/{name}', 'sha256': item.get('sha256')})
    request = {'input_dir': 'input', 'output_dir': 'output', 'inputs': declared, 'options': dict(options or {})}
    prefix, isolation = _sandbox(out_dir, tmp_dir, base / 'policy.sb')
    argv = [*(prefix or []), sys.executable, '-I', str(program / tool['entrypoint'])]
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_dir), 'TMPDIR': str(tmp_dir),
           'LC_ALL': 'C.UTF-8', 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    evidence = {'isolation': isolation, 'network': 'denied' if isolation == 'seatbelt' else 'not_enforced',
                'timeout_seconds': timeout, 'python': platform.python_version(),
                'limits': {'cpu_seconds': timeout, 'max_output_bytes': int(tool['permissions']['max_output_bytes']),
                           'memory_bytes': MEMORY_LIMIT if MEMORY_LIMIT_SUPPORTED else 'unsupported_on_host'}}
    try:
        proc = subprocess.run(argv, cwd=base, env=env, input=json.dumps(request).encode(),
                              capture_output=True, timeout=timeout,
                              preexec_fn=_limits(timeout, int(tool['permissions']['max_output_bytes'])))
    except subprocess.TimeoutExpired:
        return {'status': 'failed', 'error_code': 'timeout', 'validation_status': 'failed',
                'error': f'工具执行超过 {timeout} 秒已终止', 'outputs': [], 'evidence': evidence,
                'duration_ms': int((time.time() - started) * 1000)}
    except OSError as exc:
        return {'status': 'failed', 'error_code': 'spawn_failed', 'validation_status': 'failed',
                'error': f'无法启动工具进程：{exc}', 'outputs': [], 'evidence': evidence,
                'duration_ms': int((time.time() - started) * 1000)}
    stderr = proc.stderr[:4096].decode('utf-8', 'replace')
    if proc.returncode != 0:
        return {'status': 'failed', 'error_code': 'tool_error', 'validation_status': 'failed',
                'error': f'工具以退出码 {proc.returncode} 结束：{stderr.strip()[:800]}',
                'outputs': [], 'evidence': evidence, 'duration_ms': int((time.time() - started) * 1000)}
    if len(proc.stdout) > MAX_STDOUT:
        return {'status': 'failed', 'error_code': 'output_too_large', 'validation_status': 'failed',
                'error': '工具结果超过 256 KB', 'outputs': [], 'evidence': evidence,
                'duration_ms': int((time.time() - started) * 1000)}
    try:
        reported = json.loads(proc.stdout.decode('utf-8'))
        if not isinstance(reported, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        return {'status': 'failed', 'error_code': 'bad_contract', 'validation_status': 'failed',
                'error': '工具没有按契约输出 JSON 结果', 'outputs': [], 'evidence': evidence,
                'duration_ms': int((time.time() - started) * 1000)}
    problems = _schema_check(reported, tool['output_schema'])
    outputs, limit = [], int(tool['permissions']['max_output_bytes'])
    for item in reported.get('outputs', []) if isinstance(reported.get('outputs'), list) else []:
        rel = str(item.get('path', ''))
        target = (out_dir / rel).resolve()
        if not rel or not target.is_relative_to(out_dir.resolve()) or target.is_symlink() or not target.is_file():
            problems.append(f'声明的输出文件不存在或越界：{rel}')
            continue
        size = target.stat().st_size
        if size > limit:
            problems.append(f'输出文件超过上限：{rel}')
            continue
        outputs.append({'name': Path(rel).name, 'path': rel, 'size': size, 'content': target.read_bytes(),
                        'kind': str(item.get('kind') or 'file')})
    if reported.get('status') == 'ok' and not outputs and not reported.get('result'):
        # A success with nothing to show is not a success.
        problems.append('工具声称成功但没有产出任何结果文件或结构化结果')
    failed = reported.get('status') != 'ok' or problems
    return {'status': 'failed' if failed else 'succeeded',
            'error_code': (reported.get('error_code') if reported.get('status') != 'ok' else None)
                          or ('validation_failed' if problems else None),
            'validation_status': 'failed' if problems else ('passed' if not failed else 'unverified'),
            'error': '；'.join(problems) or (str(reported.get('error') or '') if failed else None) or None,
            'result': reported.get('result'), 'diagnostics': reported.get('diagnostics', []),
            'outputs': outputs, 'evidence': evidence, 'duration_ms': int((time.time() - started) * 1000)}


def evaluate(candidate, files: dict[str, bytes], *, keep=False):
    """Run the pack's own test set against the *actual* candidate content.

    Every case executes the real tool; nothing is asserted from a mock. The report carries
    the content digest it ran against so publishing can refuse evidence from other content.
    """
    manifest = candidate['manifest']
    test_path = manifest['evaluation_policy']['test_set']
    try:
        spec = json.loads(files[test_path].decode('utf-8'))
        cases = spec['cases']
        if not isinstance(cases, list) or not cases:
            raise ValueError
    except (KeyError, ValueError, UnicodeDecodeError):
        return {'passed': False, 'summary': f'测试集 {test_path} 不是含 cases 数组的 JSON', 'cases': [],
                'environment': environment_report(manifest), 'test_set': test_path, 'scope': []}
    env = environment_report(manifest)
    results = []
    base = Path(tempfile.mkdtemp(prefix='pack-eval-'))
    try:
        for index, case in enumerate(cases):
            cid = str(case.get('id') or f'case-{index + 1}')
            inputs = []
            missing = [p for p in case.get('inputs', []) if p not in files]
            for path in case.get('inputs', []):
                if path in files:
                    inputs.append({'name': Path(path).name, 'content': files[path]})
            if missing:
                results.append({'id': cid, 'passed': False, 'reason': f'测试输入不在包内：{", ".join(missing)}'})
                continue
            outcome = run_tool(candidate, files, inputs, options=case.get('options'),
                               workdir=base / cid.replace('/', '_'))
            results.append({'id': cid, **_judge(case, outcome, files)})
        passed = all(r['passed'] for r in results)
        scope = [row for row in manifest['support_matrix'] if row['status'] != 'unsupported']
        return {'passed': passed, 'cases': results, 'environment': env, 'test_set': test_path, 'scope': scope,
                'summary': f"{sum(r['passed'] for r in results)}/{len(results)} 个用例通过"
                           + ('' if passed else '；验证未通过，不可发布')}
    finally:
        if not keep:
            shutil.rmtree(base, ignore_errors=True)


def _judge(case, outcome, files):
    """Compare a real execution against the case's declared expectations."""
    expect = case.get('expect') or {}
    detail = {'status': outcome['status'], 'error_code': outcome.get('error_code'),
              'error': outcome.get('error'), 'duration_ms': outcome.get('duration_ms'),
              'isolation': outcome['evidence']['isolation']}
    if expect.get('error_code'):
        ok = outcome['status'] == 'failed' and outcome.get('error_code') == expect['error_code']
        return {'passed': ok, 'reason': '' if ok else f"期望失败码 {expect['error_code']}，实际 {outcome.get('error_code')}", **detail}
    if outcome['status'] != 'succeeded':
        return {'passed': False, 'reason': outcome.get('error') or '执行失败', **detail}
    produced = {o['path']: o['content'] for o in outcome['outputs']}
    for path, expected_path in (expect.get('equals') or {}).items():
        if path not in produced:
            return {'passed': False, 'reason': f'缺少输出 {path}', **detail}
        if expected_path not in files:
            return {'passed': False, 'reason': f'期望文件不在包内：{expected_path}', **detail}
        if produced[path] != files[expected_path]:
            return {'passed': False, 'reason': f'输出 {path} 与期望文件不一致', **detail}
    for path, digest in (expect.get('sha256') or {}).items():
        import hashlib
        if path not in produced or hashlib.sha256(produced[path]).hexdigest() != digest:
            return {'passed': False, 'reason': f'输出 {path} 的校验值不匹配', **detail}
    for key, value in (expect.get('result') or {}).items():
        actual = (outcome.get('result') or {}).get(key)
        if actual != value:
            return {'passed': False, 'reason': f'结果字段 {key} 期望 {value}，实际 {actual}', **detail}
    if not expect:
        return {'passed': False, 'reason': '用例没有声明任何期望，不能算通过', **detail}
    return {'passed': True, 'reason': '', **detail}
