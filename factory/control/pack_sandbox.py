"""职能包工具执行的隔离层：**没有可验证的隔离就不执行**。

与 `factory/harness/sandbox*.py`（worker 的副作用隔离）分开写，因为保证不同：
worker 必须联网、要能写整个 workspace；业务工具不许联网、只许写一个输出目录、
不许读宿主文件。把两者混在一个策略里，迟早有一方被放宽。

**可验证**不是形容词。`probe()` 每个进程真跑一次金丝雀：在沙箱里尝试写沙箱外的路径、
读沙箱外的文件、建立网络连接，三件事都必须失败，才认为隔离可用。策略写对了但没生效
（Seatbelt 少一条规则、bwrap 被 AppArmor 拦掉）看起来和生效一模一样——只有金丝雀能分开。

后端：
  - macOS: Seatbelt（`sandbox-exec`），默认拒绝，按需放开只读系统路径与运行时。
  - Linux: bubblewrap（`bwrap`），`--unshare-all` 含网络命名空间，只 ro-bind 必需路径。
  - 其他/探测失败: 不可用 → 调用方必须拒绝执行并把环境标为 unavailable。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from dataclasses import dataclass
from pathlib import Path

PROBE_TIMEOUT_S = 30.0

# Seatbelt：默认拒绝。`(literal "/")` 不可少——根目录本身读不到的话，连 /bin/echo 都
# 起不来（实测 SIGABRT，且没有任何提示指向沙箱）。路径一律走 -D 参数，不拼字符串：
# 目录名里一个引号就能提前闭合表达式，把某条 deny 挤出策略。
# Seatbelt：默认拒绝。`(literal "/")` 不可少——根目录本身读不到的话，连 /bin/echo 都
# 起不来（实测 SIGABRT，且没有任何提示指向沙箱）。路径一律走 -D 参数，不拼字符串：
# 目录名里一个引号就能提前闭合表达式，把某条 deny 挤出策略。运行时根目录有几条就写几条
# `(param "PYn")`——早先只取排序后的首末两条，Conda base + venv 的布局下正好丢掉标准库根，
# 探针报 "Could not find platform independent libraries"。
_SEATBELT_HEAD = """(version 1)
(deny default)
(allow process-fork process-exec)
(allow sysctl-read)
(allow mach-lookup mach-priv-host-port)
(allow signal (target self))
(allow file-read*
  (literal "/")
  (subpath "/usr") (subpath "/System") (subpath "/bin") (subpath "/sbin")
  (subpath "/Library") (subpath "/private/etc") (subpath "/private/var/db")
  (subpath (param "BASE"))"""
_SEATBELT_TAIL = """)
(allow file-read* file-write*
  (literal "/dev/null") (literal "/dev/zero") (literal "/dev/random") (literal "/dev/urandom")
  (literal "/dev/stdout") (literal "/dev/stderr") (literal "/dev/dtracehelper") (literal "/dev/tty"))
(allow file-write* (subpath (param "OUT")) (subpath (param "TMP")))
(deny network*)
"""


def _seatbelt_profile(runtime_count: int, link_count: int) -> str:
    """按运行时根目录与符号链接祖先的条数展开策略。

    只展开我们自己生成的参数名，路径仍然只走 -D。`LNn` 是**符号链接形式的祖先目录**：
    sbpl 的 subpath 按真实路径匹配，而 exec 走的是未解析的那条路径，中间少一个链接节点
    就会以 `execvp(...) Operation not permitted` 失败（venv 建在 /tmp 下时实测到）。
    只放开这几个链接节点本身，不是 `file-read-metadata` 全放开。
    """
    roots = ''.join(f'\n  (subpath (param "PY{index}"))' for index in range(runtime_count))
    links = ''.join(f'\n  (literal (param "LN{index}"))' for index in range(link_count))
    return _SEATBELT_HEAD + roots + links + _SEATBELT_TAIL


def symlink_ancestors(paths) -> list[Path]:
    """paths 里每条路径自身及其祖先中，属于符号链接的那些节点。"""
    found: set[Path] = set()
    for path in paths:
        current = Path(path)
        for node in (current, *current.parents):
            if node == Path(node.root):
                continue
            try:
                if node.is_symlink():
                    found.add(node)
            except OSError:
                continue
    return sorted(found, key=str)


# 金丝雀：在沙箱里报告三件事的真实结果，由外面判定。它只报告，不下结论——
# 「策略加载成功」和「策略真的挡住了」是两回事。
_CANARY = r'''
import json, socket, sys
from pathlib import Path
out = {}
try:
    Path(sys.argv[1]).write_text('escaped'); out['wrote_outside'] = True
except Exception as exc:
    out['wrote_outside'] = False; out['write_error'] = type(exc).__name__
try:
    out['read_outside'] = Path(sys.argv[2]).read_text() == 'secret'
except Exception as exc:
    out['read_outside'] = False; out['read_error'] = type(exc).__name__
try:
    out['interfaces'] = sorted(name for _, name in socket.if_nameindex())
except Exception as exc:
    out['interfaces'] = []; out['interface_error'] = type(exc).__name__
try:
    connection = socket.create_connection(('1.1.1.1', 53), timeout=3); connection.close()
    out['network'] = 'connected'
except PermissionError:
    out['network'] = 'denied'
except OSError as exc:
    out['network'] = f'{type(exc).__name__}:{getattr(exc, "errno", None)}'
try:
    Path(sys.argv[3]).write_text('ok'); out['wrote_output'] = True
except Exception as exc:
    out['wrote_output'] = False; out['output_error'] = type(exc).__name__
print(json.dumps(out))
'''


class IsolationUnavailable(RuntimeError):
    """本机没有可验证的隔离。调用方必须拒绝执行，不得降级裸跑。"""


@dataclass(frozen=True)
class Isolation:
    backend: str | None
    available: bool
    reason: str
    evidence: dict

    def as_dict(self):
        return {'backend': self.backend or 'none', 'available': self.available,
                'reason': self.reason, 'evidence': self.evidence}


def backend_name() -> str | None:
    if sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists():
        return 'seatbelt'
    if sys.platform.startswith('linux') and shutil.which('bwrap'):
        return 'bwrap'
    return None


def runtime_roots() -> list[Path]:
    """运行这个解释器所需的**最小**路径集合。

    不是 `/opt` 这种整树放开，也不是「取前缀的首末两条」——Conda base 上建的 venv 里，
    `sys.prefix` 是 .venv、标准库却在 `sys.base_prefix/lib/pythonX.Y`，只取首末会把标准库
    整个漏掉（实测探针 "Could not find platform independent/dependent libraries"）。
    这里按 sysconfig 声明的位置逐条收集，符号链接前后都收（venv 里的 python 是个链接，
    exec 走的是**未解析**的那条路径），最后去掉被祖先覆盖的条目。
    """
    candidates: set[Path] = set()
    for value in (sys.base_prefix, sys.prefix, sys.base_exec_prefix, sys.exec_prefix):
        if value:
            candidates.add(Path(value))
    paths = sysconfig.get_paths()
    for key in ('stdlib', 'platstdlib', 'purelib', 'platlib', 'scripts', 'data'):
        value = paths.get(key)
        if value:
            candidates.add(Path(value))
    executable = Path(sys.executable)
    candidates.add(executable.parent)
    expanded: set[Path] = set()
    for path in candidates:
        expanded.add(path)
        try:
            expanded.add(path.resolve())
        except OSError:
            continue
    try:
        expanded.add(executable.resolve().parent)
    except OSError:
        pass
    minimal: list[Path] = []
    for path in sorted(expanded, key=lambda item: (len(item.parts), str(item))):
        if path == Path(path.root):
            continue  # 绝不把 "/" 当运行时根放开
        if not any(path.is_relative_to(kept) for kept in minimal):
            minimal.append(path)
    return minimal


def build_argv(argv, *, base: Path, output_dir: Path, tmp_dir: Path):
    """把 argv 包进本机后端。路径一律 resolve：/var 是 /private/var 的符号链接，
    sbpl 的 subpath 与 bwrap 的 bind 都按真实路径匹配，不 resolve 的话策略照样加载，
    工具却一个字节都写不出来（实测过这个失效）。"""
    backend = backend_name()
    base, output_dir, tmp_dir = base.resolve(), output_dir.resolve(), tmp_dir.resolve()
    if backend == 'seatbelt':
        roots = runtime_roots()
        links = symlink_ancestors([*roots, Path(argv[0]) if argv else base])
        profile = base / '.policy.sb'
        profile.write_text(_seatbelt_profile(len(roots), len(links)), encoding='utf-8')
        params = []
        for index, root in enumerate(roots):
            params += ['-D', f'PY{index}={root}']
        for index, link in enumerate(links):
            params += ['-D', f'LN{index}={link}']
        return ['/usr/bin/sandbox-exec', '-f', str(profile), *params, '-D', f'BASE={base}',
                '-D', f'OUT={output_dir}', '-D', f'TMP={tmp_dir}', *argv]
    if backend == 'bwrap':
        # --unshare-all 含 --unshare-net：网络不是靠策略语句禁掉的，而是根本没有接口。
        # 只 ro-bind 运行时与必需系统库，不 ro-bind 整个 /：/home、/root、/var、/opt、
        # /etc 里的凭据因此在沙箱里根本不存在，而不是「存在但被拒绝」。
        wrapped = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session',
                   '--proc', '/proc', '--dev', '/dev']
        for path in ('/usr', '/lib', '/lib64', '/bin', '/sbin', '/etc/ld.so.cache',
                     '/etc/ld.so.conf', '/etc/ld.so.conf.d'):
            wrapped += ['--ro-bind-try', path, path]
        for root in runtime_roots():
            wrapped += ['--ro-bind-try', str(root), str(root)]
        wrapped += ['--ro-bind', str(base), str(base)]          # program/ 与 input/ 只读
        wrapped += ['--bind', str(output_dir), str(output_dir)]  # 唯一可写出口
        wrapped += ['--bind', str(tmp_dir), str(tmp_dir)]
        wrapped += ['--chdir', str(base)]
        return [*wrapped, *argv]
    raise IsolationUnavailable(
        f'本机没有可用的隔离后端（platform={sys.platform}）。macOS 需要 /usr/bin/sandbox-exec，'
        'Linux 需要 bwrap（bubblewrap）且允许非特权 user namespace。')


def sandbox_env(tmp_dir: Path) -> dict[str, str]:
    """工具进程的环境：不继承任何凭据。父进程的 API key、token、代理设置一律不传。"""
    return {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_dir), 'TMPDIR': str(tmp_dir),
            'LC_ALL': 'C.UTF-8', 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}


_cached: Isolation | None = None


def probe(*, refresh: bool = False) -> Isolation:
    """真跑一次金丝雀。结果按进程缓存——它取决于本机能力，不取决于哪个包在跑。"""
    global _cached
    if _cached is not None and not refresh:
        return _cached
    _cached = _run_canary()
    return _cached


def _run_canary() -> Isolation:
    backend = backend_name()
    if backend is None:
        return Isolation(None, False, '没有可用的隔离后端（缺少 sandbox-exec / bwrap）', {})
    # resolve()：macOS 的 mkdtemp 给 /var/folders/...，真实路径是 /private/var/folders/...。
    # 策略按真实路径匹配，argv 里留符号链接形式会被拒（实测 Errno 1）。
    outside = Path(tempfile.mkdtemp(prefix='pack-canary-outside-')).resolve()
    base = Path(tempfile.mkdtemp(prefix='pack-canary-')).resolve()
    try:
        (outside / 'secret.txt').write_text('secret', encoding='utf-8')
        output_dir, tmp_dir = base / 'output', base / 'tmp'
        output_dir.mkdir(); tmp_dir.mkdir()
        script = base / 'canary.py'
        script.write_text(_CANARY, encoding='utf-8')
        argv = [sys.executable, '-I', str(script), str(outside / 'escaped.txt'),
                str(outside / 'secret.txt'), str(output_dir / 'ok.txt')]
        try:
            wrapped = build_argv(argv, base=base, output_dir=output_dir, tmp_dir=tmp_dir)
        except IsolationUnavailable as exc:
            return Isolation(backend, False, str(exc), {})
        try:
            proc = subprocess.run(wrapped, capture_output=True, timeout=PROBE_TIMEOUT_S,
                                  env=sandbox_env(tmp_dir), cwd=base)
        except (OSError, subprocess.SubprocessError) as exc:
            return Isolation(backend, False, f'隔离金丝雀无法启动：{exc}', {})
        if proc.returncode != 0:
            return Isolation(backend, False,
                             f'隔离金丝雀退出码 {proc.returncode}：{proc.stderr[:400].decode("utf-8", "replace")}', {})
        try:
            report = json.loads(proc.stdout.decode('utf-8'))
        except ValueError:
            return Isolation(backend, False, '隔离金丝雀没有返回可解析的结果', {})
        failures = []
        if report.get('wrote_outside') or (outside / 'escaped.txt').exists():
            failures.append('沙箱内写到了输出目录之外')
        if report.get('read_outside'):
            failures.append('沙箱内读到了宿主文件')
        if not report.get('wrote_output'):
            failures.append('沙箱内写不了输出目录，隔离过严无法执行工具')
        network = str(report.get('network'))
        interfaces = report.get('interfaces') or []
        if backend == 'bwrap':
            if [name for name in interfaces if name != 'lo']:
                failures.append(f'网络命名空间未隔离，仍可见接口 {interfaces}')
        elif network == 'connected':
            failures.append('沙箱内建立了对外网络连接')
        report['backend'] = backend
        if failures:
            return Isolation(backend, False, '；'.join(failures), report)
        return Isolation(backend, True, '金丝雀确认：不可越界写、不可读宿主、无对外网络', report)
    finally:
        shutil.rmtree(outside, ignore_errors=True)
        shutil.rmtree(base, ignore_errors=True)
