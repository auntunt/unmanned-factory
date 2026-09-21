"""The shared code-query layer. One backend, three business scenarios.

Why this file exists at all: ``factory/control/codegraph.py`` is a small
in-tree indexer this project wrote long before the enterprise scenarios. It
parses Python and JS/TS only, reads a fixed Git commit and never looks at the
working tree. It is a fine fallback and a bad foundation for 信创 work, where
the customer's code is Java or C#. Calling it "the code graph" and claiming
Java support would have been a rename, not an integration.

So the relation backend is an external tool -- ``@colbymchenry/codegraph``
(MIT, tree-sitter grammars in a Rust kernel, local SQLite index, CLI). This
module is the adapter: it is the only place that knows the backend exists, so
the three scenarios share one code-query layer instead of each growing a
parser.

What was actually measured, on 2026-09-21, with backend 1.6.0, against minimal
two-file cross-file fixtures (one callee file, one caller file per language):

* Java / Python / C# / Go / C++ -- the cross-file caller edge resolves.
* Rust -- symbols are indexed and findable, the cross-file call edge is **not**
  resolved (``callers`` and ``callees`` both come back empty).

Three things the probe found that this adapter has to correct for, because
each of them would otherwise turn into a confident wrong answer:

1. **``callers``/``callees`` are keyed by symbol *name*, and names collide
   across languages.** In a mixed repo, ``callers format_money`` returned a C++
   caller *and* a Python caller for what are two unrelated functions; a Rust
   query returned a C++ caller. Every result here is therefore filtered to the
   language of the definition it was asked about, and whatever got dropped is
   reported rather than silently discarded -- an invented cross-language edge
   is exactly what LANGUAGE-SUPPORT.md forbids.
2. **``--json`` is not honoured on the empty paths.** "symbol not found" and
   "project not initialized" print a human sentence and, in the second case,
   exit 1. A caller that just ``json.loads``-ed stdout would read both as a
   broken tool. They are distinguished here as ``no_match`` and
   ``not_indexed``.
3. **Truncation is silent.** ``--limit 2`` against three callers returns two
   with no indication that a third exists. This adapter over-fetches by one and
   reports ``truncated`` itself, so "that is all of them" and "that is as many
   as you asked for" stay different answers.

Indexing a project creates ``.codegraph/`` inside it. The directory carries its
own ``.gitignore`` (``*``), so its contents are invisible to git whatever we do;
the directory entry itself is added to ``.git/info/exclude`` so an indexed
project does not show up dirty and a worker's ``git add -A`` cannot commit it.
That invisibility is a real blind spot and is recorded as one -- code placed
under ``.codegraph/`` would not appear in any git-shaped review, while checks
running on the real tree could still execute it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_PACKAGE = '@colbymchenry/codegraph'
BACKEND_COMMAND = 'codegraph'
#: The version this adapter's behaviour was measured against. A different
#: version is used, not refused -- but the measured-capability claims below are
#: only evidence for this one, and ``backend_info`` reports the mismatch.
MEASURED_BACKEND_VERSION = '1.6.0'

_TIMEOUT_S = 120

# -- result vocabulary ------------------------------------------------------
#: A query never returns a bare empty list. These are the distinguishable
#: answers, because "nothing matched", "nothing is indexed", "there is more
#: than I returned" and "I cannot read this language" are four different facts
#: and a caller acts differently on each.
OK = 'ok'
NO_MATCH = 'no_match'
NOT_INDEXED = 'not_indexed'
TRUNCATED = 'truncated'
UNSUPPORTED = 'unsupported'
BACKEND_UNAVAILABLE = 'backend_unavailable'
#: Symbol/text search without relation analysis. Returned when the relation
#: backend cannot answer; never presented as relation analysis.
DEGRADED_TEXT = 'degraded_text'
#: The backend ran and failed (non-zero exit, error on stderr). This is not an
#: empty result: a database error that reads as "nothing calls this function"
#: is the most expensive kind of wrong answer this layer can give.
BACKEND_ERROR = 'backend_error'
#: The backend did not finish in time. Also not an empty result.
BACKEND_TIMEOUT = 'backend_timeout'
#: The backend exited 0 but printed something this adapter cannot parse and
#: cannot recognise as its "not found" sentence. Reported rather than guessed.
MALFORMED_OUTPUT = 'malformed_output'
#: Results are real but this layer cannot promise they are all of them: the
#: backend page came back full *before* language/path filtering, so a match it
#: never returned cannot be ruled out. Distinct from ``truncated``, which means
#: "more matched than you asked for" -- here the unknown is on the backend side.
INCOMPLETE = 'incomplete'
#: More than one definition in the same language answers to this name, so a
#: name-keyed relation query cannot say which one it is about.
AMBIGUOUS = 'ambiguous'

OUTCOMES = (OK, NO_MATCH, NOT_INDEXED, TRUNCATED, UNSUPPORTED,
            BACKEND_UNAVAILABLE, DEGRADED_TEXT, BACKEND_ERROR, BACKEND_TIMEOUT,
            MALFORMED_OUTPUT, INCOMPLETE, AMBIGUOUS)

#: Outcomes that mean "the question was not answered". A caller must not read
#: any of these as "there is nothing there".
FAILED_OUTCOMES = (BACKEND_ERROR, BACKEND_TIMEOUT, MALFORMED_OUTPUT,
                   BACKEND_UNAVAILABLE, NOT_INDEXED)

#: The one stdout shape that is a confirmed empty answer. Anything else the
#: backend prints instead of JSON is malformed output, not "no match" --
#: measured: ``ℹ Symbol "x" not found``.
_NOT_FOUND = re.compile(r'Symbol\s+"[^"]*"\s+not found', re.I)
_NOT_INITIALIZED = re.compile(r'not initialized', re.I)

#: How far past the caller's limit this layer will look so that filtering by
#: language/path still leaves a defensible answer. Bounded on purpose: the
#: review's instruction is to report an incomplete result, not to page forever.
_FILTER_OVERFETCH = 5
_MAX_PAGE = 200

#: Node kinds that can actually *be* the target of a call relation. An
#: ``import`` row naming a symbol is not a second definition of it -- treating
#: it as one made every Python lookup ambiguous with itself.
_DEFINITION_KINDS = frozenset({
    'function', 'method', 'class', 'struct', 'interface', 'enum', 'trait',
    'namespace', 'constant', 'variable', 'type', 'constructor', 'property',
})

# -- capability ledger ------------------------------------------------------
#: Evidence levels. ``fixture`` means this adapter's own test suite drives the
#: real backend over a real cross-file fixture and asserts the answer; it does
#: not mean the capability was measured on a customer repository.
FIXTURE = 'verified_on_fixture'
NOT_RESOLVED = 'not_resolved'
UNVERIFIED = 'unverified'
#: The answer depends on the project's own toolchain, so it is probed per
#: project rather than declared per language. See ``project_conditions``.
PROJECT = 'project_dependent'


@dataclass(frozen=True)
class LanguageCapability:
    """Three tiers, recorded separately.

    LANGUAGE-SUPPORT.md is explicit that one green "supported" must not cover
    all three: being able to parse a language is not being able to resolve its
    cross-file relations, and neither is being able to build and change the
    project. ``build`` is never claimed here -- it is always probed.
    """

    language: str
    label: str
    #: tier 1 -- find definitions and references
    search: str
    #: tier 2 -- cross-file callers/callees/impact
    relations: str
    #: tier 3 -- build, modify, check behaviour
    build: str
    #: relation kinds the static index is known not to resolve
    unresolved: tuple = ()
    #: what a change to this language needs identified before it can be built
    conditions: tuple = ()
    note: str = ''
    required: bool = True


#: Cross-language relations nothing in a per-file static index can see. Kept
#: separate from the per-language list because it applies to every query in a
#: mixed repository, and because inventing one of these edges is the specific
#: failure LANGUAGE-SUPPORT.md calls out.
CROSS_BOUNDARY_UNRESOLVED = (
    '跨语言调用（RPC/HTTP/消息）',
    '外部函数接口（FFI/JNI/P-Invoke）',
    '通过数据库或配置文件建立的关系',
)

LANGUAGES: dict[str, LanguageCapability] = {
    'java': LanguageCapability(
        language='java', label='Java', search=FIXTURE, relations=FIXTURE,
        build=PROJECT,
        unresolved=('反射与运行时代理', '框架的依赖注入与动态绑定（如 Spring）',
                    '注解处理器生成的代码'),
        conditions=('JDK 版本', 'Maven/Gradle 构建文件', '框架与私有依赖来源'),
        note='静态图不是运行时全貌：Spring 的注入与配置要按实际来源回查，不能由图推断。'),
    'python': LanguageCapability(
        language='python', label='Python', search=FIXTURE, relations=FIXTURE,
        build=PROJECT,
        unresolved=('动态导入（importlib/__import__）', '反射与猴子补丁',
                    '运行时注册的插件入口'),
        conditions=('Python 版本', '依赖锁定与虚拟环境', '实际构建与运行入口'),
        note='不预设仓库是 Web 应用；运行入口按项目实际情况确认。'),
    'csharp': LanguageCapability(
        language='csharp', label='C#/.NET', search=FIXTURE, relations=FIXTURE,
        build=PROJECT,
        unresolved=('P/Invoke 与 COM 互操作', '没有源码的私有 DLL',
                    'WebForms/WCF 的运行时绑定', '反射与运行时程序集加载'),
        conditions=('.sln/.csproj', '目标框架（TargetFramework）',
                    '.NET Framework 还是现代 .NET', 'Windows 专有还是跨平台'),
        note='索引读的是 .cs 语法，它不知道目标框架。'
             '.NET Framework 与现代 .NET 的区别由 .csproj 判定，'
             '不能因为 .cs 能解析就说整套框架已支持。'),
    'go': LanguageCapability(
        language='go', label='Go', search=FIXTURE, relations=FIXTURE,
        build=PROJECT,
        unresolved=('接口的动态分派', 'cgo 边界', 'go:generate 生成的代码',
                    '被 build tag 排除的分支'),
        conditions=('go.mod/go.work', 'Go 版本', 'build tags', 'cgo 环境'),
        note='适用的构建条件必须随结果记录。'),
    'cpp': LanguageCapability(
        language='cpp', label='C/C++', search=FIXTURE, relations=FIXTURE,
        build=UNVERIFIED,
        unresolved=('宏与条件编译分支', '模板实例化',
                    '依赖 include 路径/编译数据库才能确定的跨文件关系'),
        conditions=('编译数据库（compile_commands.json）', '工具链与 include 路径'),
        note='只在一个头文件加两个编译单元的最小样例上验证过；'
             '宏、条件编译与模板未验证。可选语言。',
        required=False),
    'rust': LanguageCapability(
        language='rust', label='Rust', search=FIXTURE, relations=NOT_RESOLVED,
        build=UNVERIFIED,
        unresolved=('跨文件调用（实测未解析）', '宏展开', 'trait 的动态分派',
                    'feature 条件编译'),
        conditions=('Cargo.toml/Cargo.lock', 'feature 组合', 'toolchain 版本'),
        note='实测：符号能索引能检索，但同样形状的跨文件调用边没有解析出来'
             '（callers 与 callees 都为空）。因此关系层不可用，本期后置。',
        required=False),
}

#: Extension -> language. Used to decide which capability record applies to a
#: result, and to filter out same-name symbols from another language.
EXT_LANGUAGE = {
    '.java': 'java',
    '.py': 'python', '.pyi': 'python',
    '.cs': 'csharp',
    '.go': 'go',
    '.cc': 'cpp', '.cpp': 'cpp', '.cxx': 'cpp', '.hpp': 'cpp',
    '.h': 'cpp', '.hh': 'cpp', '.c': 'cpp',
    '.rs': 'rust',
}

REQUIRED_LANGUAGES = tuple(c.language for c in LANGUAGES.values() if c.required)
OPTIONAL_LANGUAGES = tuple(c.language for c in LANGUAGES.values() if not c.required)


def language_of(path: str) -> str | None:
    return EXT_LANGUAGE.get(Path(str(path)).suffix.lower())


def capability(language: str | None) -> dict | None:
    record = LANGUAGES.get(language or '')
    if record is None:
        return None
    return {'language': record.language, 'label': record.label,
            'search': record.search, 'relations': record.relations,
            'build': record.build, 'unresolved': list(record.unresolved),
            'conditions': list(record.conditions), 'note': record.note,
            'required': record.required}


def relations_usable(language: str | None) -> bool:
    """Whether the backend actually resolved this language's relations here.

    A language whose relations were measured as unresolved must not have an
    empty result reported as "nothing calls this" -- that reads identically to
    a real answer, which is the whole reason this check exists.
    """
    record = LANGUAGES.get(language or '')
    return bool(record) and record.relations == FIXTURE


# -- backend ---------------------------------------------------------------
def backend_path() -> str | None:
    return shutil.which(BACKEND_COMMAND)


def backend_info() -> dict:
    """What is actually installed, or why nothing is."""
    path = backend_path()
    if not path:
        return {'available': False, 'package': BACKEND_PACKAGE, 'path': None,
                'version': None, 'measured_version': MEASURED_BACKEND_VERSION,
                'reason': f'没有找到 {BACKEND_COMMAND} 可执行文件，'
                          f'关系查询不可用；可用 npm i -g {BACKEND_PACKAGE} 安装'}
    try:
        done = subprocess.run([path, 'version'], capture_output=True, text=True,
                              timeout=20)
        version = (done.stdout or '').strip().splitlines()[-1].strip() if done.stdout else None
    except (subprocess.SubprocessError, OSError) as exc:
        return {'available': False, 'package': BACKEND_PACKAGE, 'path': path,
                'version': None, 'measured_version': MEASURED_BACKEND_VERSION,
                'reason': f'{BACKEND_COMMAND} 无法执行：{exc}'}
    return {'available': True, 'package': BACKEND_PACKAGE, 'path': path,
            'version': version, 'measured_version': MEASURED_BACKEND_VERSION,
            'version_matches_measurement': version == MEASURED_BACKEND_VERSION,
            'reason': None}


@dataclass(frozen=True)
class _Invocation:
    """One backend call, with the failure modes kept apart.

    The first version returned ``None`` for every unhappy path and then treated
    any unparseable stdout as "no match". A non-zero exit, a timeout and a
    malformed line all became "nothing found", which is the failure the review
    reproduced: an injected ``exit 1`` with ``stderr='database error'`` came
    back as a confident empty result.
    """

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None


def _run(root, args, *, timeout=_TIMEOUT_S) -> _Invocation:
    path = backend_path()
    if not path:
        return _Invocation(None, '', '', error='后端不可用')
    env = dict(os.environ, NO_COLOR='1')
    try:
        done = subprocess.run([path, *args], cwd=str(root), env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _Invocation(None, '', '', timed_out=True,
                           error=f'后端 {" ".join(args[:1])} 超过 {timeout}s 未返回')
    except (subprocess.SubprocessError, OSError) as exc:
        return _Invocation(None, '', '', error=f'后端无法执行：{exc}')
    return _Invocation(done.returncode, done.stdout or '', done.stderr or '')


def _classify(done: _Invocation):
    """``(payload, outcome, reason)``. Exactly one of payload/outcome is set.

    Only the backend's own confirmed "not found" sentence is allowed to become
    ``no_match``. Everything else it prints instead of JSON is reported as
    malformed output, because this adapter cannot tell an empty answer from a
    broken one by looking at prose it does not recognise.
    """
    if done.timed_out:
        return None, BACKEND_TIMEOUT, done.error
    if done.returncode is None:
        return None, BACKEND_UNAVAILABLE, done.error
    text = (done.stdout or '').strip()
    noise = (done.stderr or '').strip()
    if done.returncode != 0:
        if _NOT_INITIALIZED.search(text) or _NOT_INITIALIZED.search(noise):
            return None, NOT_INDEXED, (text or noise or '这个项目还没有建立代码索引')
        return None, BACKEND_ERROR, (
            f'后端退出码 {done.returncode}：{noise or text or "没有输出"}')
    if text[:1] in ('{', '['):
        try:
            return json.loads(text), None, None
        except json.JSONDecodeError as exc:
            return None, MALFORMED_OUTPUT, f'后端返回的 JSON 无法解析：{exc}'
    if _NOT_FOUND.search(text):
        return None, NO_MATCH, text
    if _NOT_INITIALIZED.search(text):
        return None, NOT_INDEXED, text
    return None, MALFORMED_OUTPUT, (
        f'后端退出码 0 但输出既不是 JSON 也不是已知的「未找到」提示：'
        f'{(text or noise or "空输出")[:200]}')


def index_home() -> Path:
    """Where indexing working copies live -- never inside the execution tree.

    ``.git/info/exclude`` was the previous answer and it was not isolation: it
    hid a directory from ``git status``, which is not the same as the directory
    not being there. The index now lives in its own git worktree under this
    directory, so the workspace an executor edits never contains a
    ``.codegraph/`` at all and nothing has to be hidden from anyone.
    """
    raw = os.getenv('FACTORY_CODE_INDEX_HOME') or '~/.factory/control/code-index'
    return Path(raw).expanduser()


def _git(root, *args, timeout=60):
    try:
        done = subprocess.run(['git', *args], cwd=str(root), text=True,
                              capture_output=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return None
    return done


def _head(root) -> str | None:
    done = _git(root, 'rev-parse', 'HEAD')
    return done.stdout.strip() if done and done.returncode == 0 else None


def index_workspace(source_root, *, create=False) -> dict:
    """The indexing working copy for a source tree, and the commit it is at.

    One worktree per source tree, checked out detached at a pinned commit and
    re-pointed (not re-created) when that commit moves, so the backend's
    incremental ``sync`` still applies and the mapping stays one-to-one.

    The cost is stated rather than hidden: this indexes a **commit**, so
    uncommitted edits in the execution tree are not in the index. Every answer
    therefore carries both ``indexed_commit`` and the source tree's own
    ``source_commit``/``source_dirty``, which is the version mapping the review
    asked for -- and it is also what makes "rebuild the delivery from the
    reviewed baseline" checkable.
    """
    source = Path(str(source_root)).resolve()
    if not source.is_dir():
        return {'ok': False, 'reason': f'源码目录不存在：{source}'}
    source_commit = _head(source)
    if not source_commit:
        return {'ok': False, 'reason': '源码目录不是一个可用的 git 仓库，'
                                       '无法建立隔离的索引工作副本',
                'source_commit': None}
    digest = hashlib.sha256(str(source).encode()).hexdigest()[:16]
    base = index_home() / digest
    tree, meta_path = base / 'tree', base / 'meta.json'
    status = _git(source, 'status', '--porcelain')
    dirty = bool(status.stdout.strip()) if status and status.returncode == 0 else None
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            meta = {}
    indexed_commit = meta.get('indexed_commit') if tree.is_dir() else None

    if create:
        base.mkdir(parents=True, exist_ok=True)
        if not tree.is_dir():
            done = _git(source, 'worktree', 'add', '--detach', str(tree),
                        source_commit, timeout=300)
            if done is None or done.returncode != 0:
                detail = (done.stderr.strip() if done else '没有返回')
                return {'ok': False, 'reason': f'建立索引工作副本失败：{detail}',
                        'source_commit': source_commit, 'source_dirty': dirty}
        elif indexed_commit != source_commit:
            done = _git(tree, 'checkout', '--detach', source_commit, timeout=300)
            if done is None or done.returncode != 0:
                detail = (done.stderr.strip() if done else '没有返回')
                return {'ok': False, 'reason': f'索引工作副本无法切到 {source_commit[:12]}：{detail}',
                        'source_commit': source_commit, 'source_dirty': dirty,
                        'root': str(tree), 'indexed_commit': indexed_commit}
        indexed_commit = source_commit
        meta_path.write_text(json.dumps(
            {'source_root': str(source), 'indexed_commit': indexed_commit,
             'updated_at': _now_iso()}, ensure_ascii=False), encoding='utf-8')

    return {'ok': tree.is_dir(), 'root': str(tree),
            'reason': None if tree.is_dir() else '尚未建立隔离的索引工作副本',
            'indexed_commit': indexed_commit, 'source_commit': source_commit,
            'source_dirty': dirty,
            # The index is about a different commit than the tree an executor
            # would edit. Never silently: this is what the caller reports.
            'mapping_stale': bool(indexed_commit and indexed_commit != source_commit),
            'meta': str(meta_path)}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def code_version(root) -> dict:
    """The commit the answer is about, and whether the tree has moved off it.

    The backend indexes the working tree, not a commit, so ``dirty`` is part of
    the answer rather than a footnote: a relation that only exists in uncommitted
    code is a real relation, and one that was deleted there is really gone.
    """
    def git(*args):
        try:
            done = subprocess.run(['git', *args], cwd=str(root), text=True,
                                  capture_output=True, timeout=20)
        except (subprocess.SubprocessError, OSError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    commit = git('rev-parse', 'HEAD')
    status = git('status', '--porcelain')
    return {'commit': commit,
            'dirty': bool(status) if status is not None else None,
            'branch': git('rev-parse', '--abbrev-ref', 'HEAD')}


def index_status(root) -> dict:
    """Index freshness, or why there is none. Never a fabricated 'fresh'."""
    info = backend_info()
    if not info['available']:
        return {'indexed': False, 'outcome': BACKEND_UNAVAILABLE,
                'reason': info['reason'], 'backend': info, 'mapping': {}}
    mapping = index_workspace(root)
    if not mapping['ok']:
        return {'indexed': False, 'outcome': NOT_INDEXED,
                'reason': mapping['reason'], 'backend': info, 'mapping': mapping}
    done = _run(mapping['root'], ['status', '--json'])
    payload, outcome, reason = _classify(done)
    if payload is None:
        return {'indexed': False, 'outcome': outcome, 'reason': reason,
                'backend': info, 'mapping': mapping}
    if not payload.get('initialized'):
        return {'indexed': False, 'outcome': NOT_INDEXED,
                'reason': '这个项目还没有建立代码索引', 'backend': info,
                'mapping': mapping}
    pending = payload.get('pendingChanges') or {}
    changed = sum(int(pending.get(k) or 0) for k in ('added', 'modified', 'removed'))
    index = payload.get('index') or {}
    return {
        'indexed': True, 'outcome': OK, 'reason': None, 'backend': info,
        'mapping': mapping,
        'last_indexed': payload.get('lastIndexed'),
        'languages': list(payload.get('languages') or []),
        'file_count': payload.get('fileCount'), 'node_count': payload.get('nodeCount'),
        'edge_count': payload.get('edgeCount'),
        'pending_changes': {'added': pending.get('added', 0),
                            'modified': pending.get('modified', 0),
                            'removed': pending.get('removed', 0)},
        # Two different kinds of stale, both reported: the indexing copy has
        # moved off what was indexed, or the execution tree has moved off the
        # commit the indexing copy is pinned to.
        'stale': bool(changed) or bool(payload.get('worktreeMismatch'))
                 or mapping['mapping_stale'],
        'mapping_stale': mapping['mapping_stale'],
        'indexed_commit': mapping['indexed_commit'],
        'source_commit': mapping['source_commit'],
        'source_dirty': mapping['source_dirty'],
        'worktree_mismatch': payload.get('worktreeMismatch'),
        'reindex_recommended': bool(index.get('reindexRecommended')),
        'index_state': index.get('state'),
    }


def ensure_indexed(root, *, refresh=True) -> dict:
    """Build or refresh the index, in the isolated working copy."""
    info = backend_info()
    if not info['available']:
        return {'indexed': False, 'outcome': BACKEND_UNAVAILABLE,
                'reason': info['reason'], 'backend': info, 'mapping': {}}
    # Read the mapping before moving it, because the backend cannot be trusted
    # to notice the move on its own: measured, a ``git checkout`` inside the
    # indexing worktree leaves ``pendingChanges`` at zero and ``fileCount`` at
    # the old value, so a file that arrived with the new commit is simply
    # missing from the index while everything reports "fresh". The commit the
    # mapping was at is therefore the authoritative refresh signal.
    previous = index_workspace(root).get('indexed_commit')
    mapping = index_workspace(root, create=True)
    if not mapping['ok']:
        return {'indexed': False, 'outcome': NOT_INDEXED,
                'reason': mapping['reason'], 'backend': info, 'mapping': mapping}
    moved = previous != mapping.get('indexed_commit')
    status = index_status(root)
    if status['outcome'] in (BACKEND_UNAVAILABLE, BACKEND_ERROR, BACKEND_TIMEOUT,
                             MALFORMED_OUTPUT):
        return status
    if not status['indexed']:
        # ``init``/``sync`` print human progress, not JSON. Judging them with
        # the JSON classifier would report a perfectly good index build as
        # malformed output -- the exit code is the only signal they give.
        outcome, reason = _ran_cleanly(
            _run(mapping['root'], ['init', '--yes', '.'], timeout=_TIMEOUT_S * 4))
        if outcome is not None:
            return {'indexed': False, 'outcome': outcome,
                    'reason': f'建立索引失败：{reason}', 'backend': info,
                    'mapping': mapping}
        return index_status(root)
    if refresh and (moved or status['stale']):
        # ``index`` rather than ``sync`` when the checkout moved: sync trusts the
        # backend's own change detection, which is exactly what did not see the
        # checkout. A full rebuild of one project's index is seconds.
        command = ['index', '.'] if moved else ['sync', '.']
        outcome, reason = _ran_cleanly(
            _run(mapping['root'], command, timeout=_TIMEOUT_S * 4))
        if outcome is not None:
            # A refresh that failed must not leave the caller reading a stale
            # index as a current one.
            return {**status, 'outcome': outcome,
                    'reason': f'索引刷新失败：{reason}', 'stale': True}
        return index_status(root)
    return status


def _ran_cleanly(done: _Invocation):
    """``(None, None)`` when a non-JSON command succeeded, else the failure."""
    if done.timed_out:
        return BACKEND_TIMEOUT, done.error
    if done.returncode is None:
        return BACKEND_UNAVAILABLE, done.error
    if done.returncode != 0:
        return BACKEND_ERROR, (
            f'后端退出码 {done.returncode}：'
            f'{(done.stderr or done.stdout or "没有输出").strip()[:300]}')
    return None, None


def _query_backend(root, args, *, limit):
    """Run a listing command with a bounded over-fetch.

    The backend has no language filter and no pagination, so this layer asks
    for more than the caller wants and then filters. Bounded on purpose: the
    review's instruction is to report an incomplete answer, not to page until
    the process runs out of memory. ``saturated`` is the fact that matters --
    when the page came back exactly full, something may have been cut off
    *before* filtering, and neither "no match" nor "not truncated" can be
    claimed from what is left.
    """
    page = min(max(int(limit), 1) * _FILTER_OVERFETCH + 1, _MAX_PAGE)
    done = _run(root, [*args, '--json', '--limit', str(page)])
    payload, outcome, reason = _classify(done)
    return payload, outcome, reason, page


def _envelope(outcome, *, root, reason=None, results=None, language=None,
              limit=None, truncated=False, dropped=None, status=None,
              complete=True, target=None, candidates=None):
    """One shape for every answer, including the ones that failed.

    ``answered`` exists because the expensive mistake this layer can make is to
    hand back ``results: []`` for a backend error and have a caller read it as
    "there is nothing there". ``complete`` exists because a page that filled up
    before filtering cannot support either "no match" or "not truncated".
    """
    status = status if status is not None else index_status(root)
    record = LANGUAGES.get(language or '')
    mapping = status.get('mapping') or {}
    return {
        'outcome': outcome,
        'reason': reason,
        'results': list(results or []),
        'truncated': bool(truncated),
        # False when the backend's own page filled up before this layer filtered
        # it, so a match it never returned cannot be ruled out. ``truncated``
        # and ``complete`` answer different questions and are both needed.
        'complete': bool(complete),
        # True only when the question was actually answered. A caller that reads
        # ``results == []`` without checking this is reading a failure as a fact.
        'answered': outcome not in FAILED_OUTCOMES,
        # The definition a relation query was resolved against, when it could be
        # pinned to exactly one.
        'target': target,
        # The competing same-language definitions, when it could not.
        'candidates': list(candidates or []),
        'limit': limit,
        'language': language,
        'capability': capability(language),
        # What the static index is known not to see. Carried on every answer,
        # because the caller has to be able to tell "no caller" from "no caller
        # that a static index can see".
        'unresolved': list(record.unresolved) + list(CROSS_BOUNDARY_UNRESOLVED)
                      if record else list(CROSS_BOUNDARY_UNRESOLVED),
        # Same-name symbols in other languages that were dropped rather than
        # reported as relations.
        'dropped_other_language': list(dropped or []),
        'code_version': code_version(root),
        # The version mapping: which commit the isolated index is actually at,
        # versus where the execution tree is now. Uncommitted edits are not in
        # the index, and this is where that is said out loud.
        'index_mapping': {
            'indexed_commit': mapping.get('indexed_commit'),
            'source_commit': mapping.get('source_commit'),
            'source_dirty': mapping.get('source_dirty'),
            'mapping_stale': mapping.get('mapping_stale'),
            'index_root': mapping.get('root'),
        },
        'freshness': {k: status.get(k) for k in
                      ('last_indexed', 'stale', 'mapping_stale', 'pending_changes',
                       'reindex_recommended', 'index_state', 'languages')},
        'backend': status.get('backend') or backend_info(),
    }


def _json_or_none(done):
    """Kept for callers that still hold a raw invocation; classification only."""
    payload, _, _ = _classify(done)
    return payload


def definitions(root, name, *, language=None, limit=20, auto_index=True) -> dict:
    """Where a symbol is defined. Tier 1; available for every indexed language.

    ``auto_index=False`` for callers on a read path: building an index is
    minutes of work on a real customer repository, so a plain lookup reports
    ``not_indexed`` instead of doing it behind the caller's back.
    """
    status = ensure_indexed(root) if auto_index else index_status(root)
    if not status.get('indexed'):
        return _envelope(status['outcome'], root=root, reason=status['reason'],
                         language=language, limit=limit, status=status,
                         complete=False)
    payload, outcome, reason, page = _query_backend(
        status['mapping']['root'], ['query', name], limit=limit)
    if payload is None:
        return _envelope(outcome, root=root, language=language, limit=limit,
                         reason=reason, status=status,
                         complete=(outcome == NO_MATCH))
    nodes = [item.get('node') or {} for item in payload]
    saturated = len(nodes) >= page
    dropped = _drop_other_languages(nodes, language)
    kept = [n for n in nodes if language is None or n.get('language') == language]
    results = [{'name': n.get('name'), 'qualified_name': n.get('qualifiedName'),
                'id': n.get('id'), 'kind': n.get('kind'),
                'language': n.get('language'),
                'path': n.get('filePath'), 'line': n.get('startLine'),
                'end_line': n.get('endLine'), 'signature': n.get('signature')}
               for n in kept[:limit]]
    truncated = len(kept) > limit
    if not results:
        # An empty result after filtering a full page proves nothing.
        return _envelope(INCOMPLETE if saturated else NO_MATCH, root=root,
                         language=language, limit=limit, dropped=dropped,
                         status=status, complete=not saturated,
                         reason=(f'后端一页 {page} 条已取满，过滤后本语言没有命中；'
                                 '不能据此断定没有匹配' if saturated
                                 else f'没有找到符号 {name}'))
    return _envelope(INCOMPLETE if (saturated and not truncated) else
                     (TRUNCATED if truncated else OK),
                     root=root, results=results, language=language, limit=limit,
                     truncated=truncated, dropped=dropped, status=status,
                     complete=not saturated,
                     reason=('后端一页已取满，过滤后的这批可能不是全部'
                             if saturated else None))


def _drop_other_languages(nodes, language):
    if language is None:
        return []
    counts: dict[str, int] = {}
    for node in nodes:
        other = node.get('language')
        if other and other != language:
            counts[other] = counts.get(other, 0) + 1
    return [{'language': k, 'count': v} for k, v in sorted(counts.items())]


def _resolve_target(root, symbol, *, language, target_path, limit, auto_index):
    """Pin the one definition a relation query is about, or refuse to guess.

    ``callers``/``callees`` are keyed by a bare name. Filtering the *callers*
    by language does not pin the *callee*: two methods in the same language can
    share a name, and answering for "whichever one the backend matched" is a
    precise-looking claim about an unidentified target. When more than one
    candidate survives, this returns ``ambiguous`` with the candidates so the
    caller can pass ``target_path`` -- it does not pick one.
    """
    found = definitions(root, symbol, language=language, limit=max(limit, 10),
                        auto_index=auto_index)
    if found['outcome'] in FAILED_OUTCOMES:
        return None, found
    definitions_only = [r for r in found['results']
                        if (r.get('kind') or '') in _DEFINITION_KINDS]
    # Fall back to everything only when nothing looked like a definition, so a
    # kind this list has not met yet degrades to the old behaviour rather than
    # to "symbol not found".
    pool = definitions_only or found['results']
    candidates = [r for r in pool
                  if not target_path or str(r['path'] or '') == target_path
                  or str(r['path'] or '').endswith('/' + target_path.lstrip('/'))]
    if not candidates:
        return None, found
    if len(candidates) > 1:
        return None, _envelope(
            AMBIGUOUS, root=root, language=language, limit=limit,
            candidates=candidates, complete=found['complete'],
            reason=(f'同一语言里有 {len(candidates)} 个定义都叫 {symbol}，'
                    '仅凭名字无法锁定调用关系的目标；请用 target_path 指定一个'))
    return candidates[0], None


def _relation(root, verb, symbol, *, language, path_prefix, limit,
              auto_index=True, target_path=None):
    """``callers``/``callees`` share every rule, so they share one body."""
    if language is not None and not relations_usable(language):
        record = LANGUAGES.get(language)
        return _envelope(
            UNSUPPORTED, root=root, language=language, limit=limit, complete=False,
            reason=(f'{record.label} 的跨文件关系在本后端上实测没有解析出来，'
                    '这里不返回空结果冒充「没有调用方」' if record
                    else f'不支持的语言：{language}'))
    status = ensure_indexed(root) if auto_index else index_status(root)
    if not status.get('indexed'):
        return _envelope(status['outcome'], root=root, reason=status['reason'],
                         language=language, limit=limit, status=status,
                         complete=False)
    target, refusal = _resolve_target(root, symbol, language=language,
                                      target_path=target_path, limit=limit,
                                      auto_index=False)
    if refusal is not None:
        return refusal

    payload, outcome, reason, page = _query_backend(
        status['mapping']['root'], [verb, symbol], limit=limit)
    if payload is None:
        return _envelope(outcome, root=root, language=language, limit=limit,
                         reason=reason, status=status, target=target,
                         complete=(outcome == NO_MATCH))
    key = 'callers' if verb == 'callers' else 'callees'
    raw = payload.get(key) or []
    saturated = len(raw) >= page
    # The backend matches on name alone, so a mixed repository will hand back a
    # same-named symbol from another language. Filtering by the file's own
    # language is what keeps this from inventing a cross-language edge.
    tagged = [{**item, '_language': language_of(item.get('filePath') or '')}
              for item in raw]
    dropped = _drop_other_languages(
        [{'language': t['_language']} for t in tagged], language)
    kept = [t for t in tagged
            if (language is None or t['_language'] == language)
            and (not path_prefix or str(t.get('filePath') or '').startswith(path_prefix))]
    truncated = len(kept) > limit
    results = [{'name': t.get('name'), 'kind': t.get('kind'),
                'path': t.get('filePath'), 'line': t.get('startLine'),
                'language': t['_language']}
               for t in kept[:limit]]
    if not results:
        return _envelope(INCOMPLETE if saturated else NO_MATCH, root=root,
                         language=language, limit=limit, dropped=dropped,
                         status=status, target=target, complete=not saturated,
                         reason=(f'后端一页 {page} 条已取满，过滤后本次范围内没有'
                                 f'{key}；不能据此断定没有调用方' if saturated
                                 else f'{symbol} 在本次范围内没有可解析的{key}'))
    return _envelope(INCOMPLETE if (saturated and not truncated) else
                     (TRUNCATED if truncated else OK),
                     root=root, results=results, language=language, limit=limit,
                     truncated=truncated, dropped=dropped, status=status,
                     target=target, complete=not saturated,
                     reason=('后端一页已取满，过滤后的这批可能不是全部'
                             if saturated else None))


def callers(root, symbol, *, language=None, path_prefix=None, limit=20,
            auto_index=True, target_path=None) -> dict:
    return _relation(root, 'callers', symbol, language=language,
                     path_prefix=path_prefix, limit=limit,
                     auto_index=auto_index, target_path=target_path)


def callees(root, symbol, *, language=None, path_prefix=None, limit=20,
            auto_index=True, target_path=None) -> dict:
    return _relation(root, 'callees', symbol, language=language,
                     path_prefix=path_prefix, limit=limit,
                     auto_index=auto_index, target_path=target_path)


def text_fallback(root, needle, *, limit=20) -> dict:
    """Plain text search, labelled as such.

    Reached when the relation backend is unavailable. It is returned under
    ``degraded_text`` and never under ``ok``: a grep result presented as
    relation analysis is the failure LANGUAGE-SUPPORT.md names outright.
    """
    try:
        done = subprocess.run(
            ['git', 'grep', '-n', '--fixed-strings', '-I', needle],
            cwd=str(root), capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as exc:
        return _envelope(BACKEND_UNAVAILABLE, root=root,
                         reason=f'文本检索也不可用：{exc}', limit=limit)
    hits = []
    for line in (done.stdout or '').splitlines()[:limit]:
        path, _, rest = line.partition(':')
        number, _, text = rest.partition(':')
        hits.append({'path': path, 'line': int(number) if number.isdigit() else None,
                     'text': text.strip()[:200], 'language': language_of(path)})
    if not hits:
        return _envelope(NO_MATCH, root=root, reason='文本检索没有命中', limit=limit)
    return _envelope(DEGRADED_TEXT, root=root, results=hits, limit=limit,
                     truncated=len(done.stdout.splitlines()) > limit,
                     reason='关系后端不可用，这里只是文本检索，不是关系分析')


# -- project conditions ----------------------------------------------------
#: What each language needs present before "build and change it" is even a
#: question. Probed, never assumed.
_TOOLCHAIN = {
    'java': (('javac', '--version'), ('mvn', '-v'), ('gradle', '-v')),
    'python': (('python3', '--version'),),
    'csharp': (('dotnet', '--info'),),
    'go': (('go', 'version'),),
    'cpp': (('cc', '--version'),),
    'rust': (('cargo', '--version'),),
}

_MARKERS = {
    'java': ('pom.xml', 'build.gradle', 'build.gradle.kts'),
    'python': ('pyproject.toml', 'requirements.txt', 'setup.py', 'Pipfile'),
    'csharp': ('*.sln', '*.csproj'),
    'go': ('go.mod', 'go.work'),
    'cpp': ('CMakeLists.txt', 'compile_commands.json', 'Makefile'),
    'rust': ('Cargo.toml',),
}


def _found_markers(root, patterns):
    root = Path(str(root))
    found = []
    for pattern in patterns:
        if '*' in pattern:
            found += [str(p.relative_to(root)) for p in root.rglob(pattern)
                      if '.codegraph' not in p.parts][:5]
        elif (root / pattern).exists():
            found.append(pattern)
    return found


def project_conditions(root) -> dict:
    """Which languages this project actually contains, and whether we could build them.

    This is the tier-3 answer, and it is deliberately separate from the tier-1
    and tier-2 claims: an index that reads C# perfectly says nothing about
    whether a .NET SDK exists on this machine, and for .NET Framework the
    answer also depends on the operating system.
    """
    status = index_status(root)
    present = set(status.get('languages') or [])
    # 'c' and 'cpp' are one capability record here.
    if 'c' in present:
        present.discard('c')
        present.add('cpp')
    languages = []
    for name in sorted(present | {l for l in LANGUAGES if False}):
        record = LANGUAGES.get(name)
        if record is None:
            languages.append({'language': name, 'known': False,
                              'note': '不在本层登记的语言范围内，只有文本检索'})
            continue
        tools = []
        for command, *args in _TOOLCHAIN.get(name, ()):
            path = shutil.which(command)
            version = None
            if path:
                try:
                    done = subprocess.run([path, *args], capture_output=True,
                                          text=True, timeout=30)
                    version = (done.stdout or done.stderr or '').strip().splitlines()[:1]
                    version = version[0] if version else None
                except (subprocess.SubprocessError, OSError):
                    version = None
            tools.append({'command': command, 'present': bool(path), 'version': version})
        markers = _found_markers(root, _MARKERS.get(name, ()))
        buildable = any(t['present'] for t in tools)
        entry = {'language': name, 'known': True, 'label': record.label,
                 'capability': capability(name), 'toolchain': tools,
                 'markers': markers,
                 'build': 'toolchain_present' if buildable else 'toolchain_missing',
                 'missing': [t['command'] for t in tools if not t['present']]}
        if name == 'csharp':
            # The index cannot tell these apart; only the project file can, and
            # .NET Framework additionally needs Windows.
            entry['dotnet_flavour'] = _dotnet_flavour(root, markers)
        languages.append(entry)
    return {'languages': languages,
            'indexed': status.get('indexed', False),
            'reason': status.get('reason'),
            'backend': status.get('backend') or backend_info(),
            'code_version': code_version(root)}


def _dotnet_flavour(root, markers) -> dict:
    """.NET Framework vs modern .NET, read from the project files only.

    ``<TargetFrameworkVersion>v4.8</TargetFrameworkVersion>`` is .NET Framework
    and needs Windows; ``<TargetFramework>net8.0</TargetFramework>`` is modern
    .NET. When neither is found the answer is "unknown", not a guess -- the
    difference decides whether a migration slice can even be built here.
    """
    root = Path(str(root))
    frameworks, legacy, modern = [], False, False
    for marker in markers:
        path = root / marker
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        for tag in ('TargetFrameworkVersion', 'TargetFramework', 'TargetFrameworks'):
            start = f'<{tag}>'
            if start in text:
                value = text.split(start, 1)[1].split('<', 1)[0].strip()
                frameworks.append({'file': marker, 'tag': tag, 'value': value})
                if tag == 'TargetFrameworkVersion' or value.startswith('v'):
                    legacy = True
                elif value.startswith(('net4', 'net3', 'net2')) and '.' not in value[3:]:
                    legacy = True
                else:
                    modern = True
    if legacy and not modern:
        flavour = 'dotnet_framework'
    elif modern and not legacy:
        flavour = 'modern_dotnet'
    elif modern and legacy:
        flavour = 'mixed'
    else:
        flavour = 'unknown'
    return {'flavour': flavour, 'evidence': frameworks,
            'requires_windows': flavour in ('dotnet_framework', 'mixed'),
            'note': ('目标框架无法从索引判断，只能读 .csproj/.sln；'
                     '没有读到就是 unknown，不猜。')}


def capability_report() -> dict:
    """What this layer claims, for the plugin/package display.

    Every claim here is the measured one. ``build`` is never asserted per
    language -- it comes from ``project_conditions`` for a specific project.
    """
    return {
        'backend': backend_info(),
        'required': [capability(l) for l in REQUIRED_LANGUAGES],
        'optional': [capability(l) for l in OPTIONAL_LANGUAGES],
        'outcomes': list(OUTCOMES),
        'cross_boundary_unresolved': list(CROSS_BOUNDARY_UNRESOLVED),
        'evidence': ('各语言的检索与关系层由 tests/test_code_intel.py 用真实后端'
                     '在最小跨文件样例上驱动验证；这不等于在客户仓库上验证过。'),
    }
