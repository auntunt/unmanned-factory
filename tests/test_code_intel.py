"""The shared code-query layer, driven against the real backend.

Scope is deliberately small, per LANGUAGE-SUPPORT.md: one representative
cross-file relation query per required language, and one refresh after a change.
No benchmark matrix, no per-framework combinations.

These tests call the real ``codegraph`` binary. When it is not installed they
skip with the reason rather than passing on a stub -- a green run that proved
nothing about the backend is exactly the claim this layer exists to avoid.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from factory.control import code_intel as ci
from tests.test_control_app import app_env, login  # noqa: F401

pytestmark = pytest.mark.skipif(
    not ci.backend_path(),
    reason=f'{ci.BACKEND_COMMAND} 未安装；关系后端不可用，跳过而不是假装通过')


# --- fixtures: one callee file and one caller file per language ------------
CALLEE = {
    'java': ('src/com/acme/Money.java', '''package com.acme;

public class Money {
    public static String format(long cents) {
        return String.format("%d.%02d", cents / 100, Math.abs(cents % 100));
    }
}
'''),
    'python': ('acme/money.py', '''def format_money(cents: int) -> str:
    return f"{cents // 100}.{abs(cents % 100):02d}"
'''),
    'csharp': ('Acme/Money.cs', '''namespace Acme
{
    public static class Money
    {
        public static string Format(long cents)
        {
            return $"{cents / 100}.{System.Math.Abs(cents % 100):D2}";
        }
    }
}
'''),
    'go': ('acme/money.go', '''package acme

import "fmt"

func FormatMoney(cents int64) string {
	return fmt.Sprintf("%d.%02d", cents/100, cents%100)
}
'''),
}

CALLER = {
    'java': ('src/com/acme/Report.java', '''package com.acme;

public class Report {
    public String render(long cents) {
        return "total=" + Money.format(cents);
    }
}
'''),
    'python': ('acme/report.py', '''from acme.money import format_money

def render(cents: int) -> str:
    return "total=" + format_money(cents)
'''),
    'csharp': ('Acme/Report.cs', '''namespace Acme
{
    public class Report
    {
        public string Render(long cents)
        {
            return "total=" + Money.Format(cents);
        }
    }
}
'''),
    'go': ('acme/report.go', '''package acme

func Render(cents int64) string {
	return "total=" + FormatMoney(cents)
}
'''),
}

#: The symbol whose callers we ask for, per language.
SYMBOL = {'java': 'format', 'python': 'format_money',
          'csharp': 'Format', 'go': 'FormatMoney'}

#: A second caller added mid-test, to prove the index refreshes on change.
LATER_CALLER = {
    'java': ('src/com/acme/Audit.java', '''package com.acme;

public class Audit {
    public String note(long cents) { return "audit=" + Money.format(cents); }
}
'''),
    'python': ('acme/audit.py', '''from acme.money import format_money

def note(cents: int) -> str:
    return "audit=" + format_money(cents)
'''),
    'csharp': ('Acme/Audit.cs', '''namespace Acme
{
    public class Audit
    {
        public string Note(long cents) { return "audit=" + Money.Format(cents); }
    }
}
'''),
    'go': ('acme/audit.go', '''package acme

func Note(cents int64) string {
	return "audit=" + FormatMoney(cents)
}
'''),
}


def _git(root, *args):
    subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)


def _write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def _repo(tmp_path, language):
    root = tmp_path / language
    root.mkdir(parents=True)
    _git(root, 'init', '-q', '-b', 'main')
    _git(root, 'config', 'user.email', 't@example.com')
    _git(root, 'config', 'user.name', 'T')
    for relative, text in (CALLEE[language], CALLER[language]):
        _write(root, relative, text)
    if language == 'go':
        _write(root, 'go.mod', 'module acme\n\ngo 1.21\n')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'base')
    return root


# --- tier 2, per required language ----------------------------------------
@pytest.mark.parametrize('language', ci.REQUIRED_LANGUAGES)
def test_one_cross_file_relation_per_required_language(tmp_path, language):
    """The representative query: who calls the function in the other file."""
    root = _repo(tmp_path, language)
    answer = ci.callers(root, SYMBOL[language], language=language)
    assert answer['outcome'] == ci.OK, answer
    caller_path = CALLER[language][0]
    assert any(r['path'] == caller_path for r in answer['results']), answer
    assert all(r['language'] == language for r in answer['results'])
    # The answer says which code version it is about and how fresh it is.
    assert answer['code_version']['commit']
    assert answer['freshness']['stale'] is False
    assert answer['freshness']['last_indexed']
    # And it never claims to have seen what a static index cannot see.
    assert '跨语言调用（RPC/HTTP/消息）' in answer['unresolved']
    assert answer['capability']['relations'] == ci.FIXTURE


@pytest.mark.parametrize('language', ci.REQUIRED_LANGUAGES)
def test_the_index_refreshes_after_a_change(tmp_path, language):
    """A new caller is found once it is committed, and the gap was visible first.

    The index lives in its own worktree pinned to a commit, so what it answers
    about is a reviewed baseline rather than whatever is currently on disk. The
    cost of that is real and is asserted here rather than hidden: between the
    edit and the commit the mapping reports itself stale, and an uncommitted
    caller is *not* in the index.
    """
    root = _repo(tmp_path, language)
    before = ci.callers(root, SYMBOL[language], language=language)
    assert before['outcome'] == ci.OK
    seen_before = {r['path'] for r in before['results']}
    assert before['index_mapping']['indexed_commit']
    assert before['index_mapping']['mapping_stale'] is False

    relative, text = LATER_CALLER[language]
    _write(root, relative, text)
    # Uncommitted: the execution tree has moved, the indexed baseline has not.
    uncommitted = ci.index_status(root)
    assert uncommitted['source_dirty'] is True
    assert uncommitted['mapping_stale'] is False

    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'add caller')
    # Now the mapping is genuinely behind the source commit.
    assert ci.index_status(root)['mapping_stale'] is True

    after = ci.callers(root, SYMBOL[language], language=language)
    assert after['outcome'] == ci.OK, after
    seen_after = {r['path'] for r in after['results']}
    assert relative in seen_after, after
    assert seen_before < seen_after
    assert after['index_mapping']['mapping_stale'] is False


# --- the four distinguishable answers -------------------------------------
def test_no_match_is_not_an_empty_success(tmp_path):
    root = _repo(tmp_path, 'python')
    answer = ci.callers(root, 'nothing_is_called_this', language='python')
    assert answer['outcome'] == ci.NO_MATCH
    assert answer['results'] == []
    assert answer['reason']


def test_not_indexed_is_distinguishable_from_no_match(tmp_path):
    root = tmp_path / 'bare'
    root.mkdir()
    _git(root, 'init', '-q', '-b', 'main')
    _write(root, 'note.txt', 'nothing to index here\n')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'base')
    status = ci.index_status(root)
    assert status['outcome'] == ci.NOT_INDEXED
    assert status['indexed'] is False
    assert status['reason']


def test_truncation_is_reported_rather_than_silently_cutting(tmp_path):
    """The backend truncates without saying so; this layer must say so.

    Measured: ``callers --limit 2`` against three callers returns two with no
    indicator. Over-fetching by one is what makes "that is all of them"
    different from "that is as many as you asked for".
    """
    root = _repo(tmp_path, 'java')
    _write(root, *LATER_CALLER['java'])
    _write(root, 'src/com/acme/Invoice.java', '''package com.acme;

public class Invoice {
    public String line(long cents) { return "line=" + Money.format(cents); }
}
''')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'more callers')
    full = ci.callers(root, 'format', language='java', limit=20)
    assert full['outcome'] == ci.OK and full['truncated'] is False
    assert len(full['results']) >= 3

    cut = ci.callers(root, 'format', language='java', limit=2)
    assert cut['outcome'] == ci.TRUNCATED
    assert cut['truncated'] is True
    assert len(cut['results']) == 2


def test_a_language_whose_relations_do_not_resolve_is_refused_not_emptied(tmp_path):
    """Rust indexes but its cross-file edges did not resolve on this backend.

    Returning an empty caller list for it would read exactly like "nothing
    calls this", which is the silent-degradation answer this layer exists to
    prevent. It is refused with a reason instead.
    """
    root = tmp_path / 'rusty'
    root.mkdir()
    _git(root, 'init', '-q', '-b', 'main')
    _write(root, 'src/money.rs', 'pub fn format_money(c: i64) -> String { format!("{}", c) }\n')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'base')
    answer = ci.callers(root, 'format_money', language='rust')
    assert answer['outcome'] == ci.UNSUPPORTED
    assert answer['results'] == []
    assert '没有解析出来' in answer['reason']
    assert ci.LANGUAGES['rust'].relations == ci.NOT_RESOLVED
    assert ci.LANGUAGES['rust'].search == ci.FIXTURE  # tier 1 still works


def test_rust_symbols_are_still_findable_even_though_relations_are_not(tmp_path):
    root = tmp_path / 'rusty2'
    root.mkdir()
    _git(root, 'init', '-q', '-b', 'main')
    _write(root, 'src/money.rs', 'pub fn format_money(c: i64) -> String { format!("{}", c) }\n')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'base')
    found = ci.definitions(root, 'format_money', language='rust')
    assert found['outcome'] == ci.OK, found
    assert found['results'][0]['path'] == 'src/money.rs'


# --- the cross-language hazard --------------------------------------------
def test_a_same_name_symbol_in_another_language_is_not_reported_as_a_caller(tmp_path):
    """Measured on the real backend: name matching crosses language boundaries.

    A mixed repository with ``format_money`` in both Python and C++ returned
    both languages' callers for one query. Presenting that as a relation would
    be inventing a cross-language edge from a name collision.
    """
    root = tmp_path / 'mixed'
    root.mkdir()
    _git(root, 'init', '-q', '-b', 'main')
    for relative, text in (CALLEE['python'], CALLER['python']):
        _write(root, relative, text)
    _write(root, 'native/money.h', '#pragma once\n#include <string>\nstd::string format_money(long cents);\n')
    _write(root, 'native/money.cpp', '#include "money.h"\nstd::string format_money(long cents) { return std::string(); }\n')
    _write(root, 'native/report.cpp',
           '#include "money.h"\nstd::string render(long cents) { return "total=" + format_money(cents); }\n')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'base')

    # The unbound query is what mixes them: asked by name alone, the backend
    # answers for both definitions.
    indexed = ci.ensure_indexed(root)
    assert indexed['indexed'] is True, indexed
    unbound = ci._run(indexed['mapping']['root'],
                      ['callers', 'format_money', '--json', '--limit', '20'])
    mixed = {c['filePath'] for c in json.loads(unbound.stdout)['callers']}
    assert any(p.startswith('native/') for p in mixed), mixed

    # The bound query does not: it is asked about one definition, so the C++
    # caller never arrives and there is nothing left to filter out.
    answer = ci.callers(root, 'format_money', language='python')
    assert answer['outcome'] == ci.OK, answer
    assert answer['target']['path'] == 'acme/money.py', answer
    assert all(r['language'] == 'python' for r in answer['results']), answer
    assert not any(str(r['path']).startswith('native/') for r in answer['results'])


# --- tier 3 is probed, never declared -------------------------------------
def test_dotnet_framework_and_modern_dotnet_are_told_apart_by_the_project_file(tmp_path):
    """The index reads .cs syntax; only the project file names the framework."""
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    _git(legacy, 'init', '-q', '-b', 'main')
    _write(legacy, 'App.csproj',
           '<Project><PropertyGroup><TargetFrameworkVersion>v4.8</TargetFrameworkVersion>'
           '</PropertyGroup></Project>\n')
    answer = ci._dotnet_flavour(legacy, ['App.csproj'])
    assert answer['flavour'] == 'dotnet_framework'
    assert answer['requires_windows'] is True

    modern = tmp_path / 'modern'
    modern.mkdir()
    _write(modern, 'App.csproj',
           '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
           '<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>\n')
    answer = ci._dotnet_flavour(modern, ['App.csproj'])
    assert answer['flavour'] == 'modern_dotnet'
    assert answer['requires_windows'] is False

    unknown = tmp_path / 'unknown'
    unknown.mkdir()
    _write(unknown, 'App.csproj', '<Project></Project>\n')
    # No evidence means unknown, not a guess in either direction.
    assert ci._dotnet_flavour(unknown, ['App.csproj'])['flavour'] == 'unknown'


def test_build_capability_is_probed_per_project_not_claimed_per_language(tmp_path):
    root = _repo(tmp_path, 'go')
    ci.ensure_indexed(root)
    conditions = ci.project_conditions(root)
    go = next(c for c in conditions['languages'] if c['language'] == 'go')
    assert go['capability']['build'] == ci.PROJECT
    assert go['build'] in ('toolchain_present', 'toolchain_missing')
    assert 'go.mod' in go['markers']


def test_the_index_never_lands_in_the_tree_an_executor_edits(tmp_path):
    """Isolation, not concealment.

    The first version wrote ``.codegraph/`` into the project and added it to
    ``.git/info/exclude``. Hiding a directory from ``git status`` is not the
    same as the directory not being there -- an executor could still read,
    write or execute anything under it, and no git-shaped review would show it.
    The index now lives in its own worktree outside the project entirely.
    """
    root = _repo(tmp_path, 'python')
    status = ci.ensure_indexed(root)
    assert status['indexed'] is True, status
    assert not (root / '.codegraph').exists(), '索引不能落在执行者会改的那棵树里'

    index_root = Path(status['mapping']['root'])
    assert (index_root / '.codegraph').is_dir()
    assert root.resolve() not in index_root.resolve().parents
    assert index_root.resolve() != root.resolve()

    # And the execution tree is clean without anything having to be hidden.
    porcelain = subprocess.run(['git', 'status', '--porcelain'], cwd=root,
                               capture_output=True, text=True, timeout=30)
    assert porcelain.stdout.strip() == '', porcelain.stdout
    exclude = root / '.git' / 'info' / 'exclude'
    excluded = exclude.read_text(encoding='utf-8') if exclude.exists() else ''
    assert '.codegraph' not in excluded, '不再靠 .git/info/exclude 假装隔离'

    # The mapping says which commit the answer is about.
    assert status['indexed_commit'] == status['source_commit']


# --- wiring: the shared layer is reachable from the platform ---------------
def test_the_capability_report_never_collapses_the_three_tiers(app_env):
    """One green "supported" covering index, relations and rebuild is the thing
    LANGUAGE-SUPPORT.md forbids, so the surface keeps them as three fields."""
    client, store, svc, repo = app_env
    headers = login(client)
    report = client.get('/api/v2/code-intel/capabilities', headers=headers)
    assert report.status_code == 200, report.text
    body = report.json()
    required = {c['language']: c for c in body['required']}
    assert set(required) == {'java', 'python', 'csharp', 'go'}
    for capability in required.values():
        assert {'search', 'relations', 'build'} <= set(capability)
        # Build is never asserted per language; it is probed per project.
        assert capability['build'] == ci.PROJECT
    optional = {c['language']: c for c in body['optional']}
    assert optional['rust']['relations'] == ci.NOT_RESOLVED
    assert optional['cpp']['required'] is False
    # C# must say which .NET it is talking about.
    assert any('.NET Framework' in c for c in required['csharp']['conditions'])
    assert set(body['outcomes']) >= {ci.NO_MATCH, ci.NOT_INDEXED, ci.TRUNCATED,
                                     ci.UNSUPPORTED}


def test_building_the_index_builds_the_shared_layer_too(app_env):
    """One user action, both indexes.

    If the shared layer were left to a separate call, a 信创 lookup would fall
    through to the legacy indexer -- which cannot read Java or C# at all -- and
    report an empty result that looks exactly like "no matches".
    """
    client, store, svc, repo = app_env
    headers = login(client)
    from tests.test_control_app import project as make_project
    p = make_project(client, repo, headers)
    built = client.post(f'/api/v2/projects/{p["id"]}/code-index', headers=headers)
    assert built.status_code == 200, built.text
    body = built.json()
    # The legacy snapshot says out loud which languages it reads.
    assert body['covers'] == ['python', 'javascript', 'typescript']
    shared = body['shared_layer']
    assert shared['indexed'] is True, shared
    assert shared['backend']['package'] == ci.BACKEND_PACKAGE
    assert client.get(f'/api/v2/projects/{p["id"]}/code-index',
                      headers=headers).json()['shared_layer']['indexed'] is True


# --- the three defects the cb74bab review reproduced -----------------------
def test_a_backend_failure_is_never_reported_as_an_empty_result(tmp_path, monkeypatch):
    """exit 1 / timeout / unparseable output must not read as "nothing found".

    The review's probe: make ``_run`` return exit 1 with ``stderr='database
    error'`` and ``definitions`` came back ``no_match`` -- a database fault
    presented as a fact about the code.
    """
    root = _repo(tmp_path, 'python')
    ci.ensure_indexed(root)
    real = ci._run

    def failing(index_root, args, **kwargs):
        if args[:1] in (['query'], ['callers'], ['callees']):
            return ci._Invocation(1, '', 'database error')
        return real(index_root, args, **kwargs)

    monkeypatch.setattr(ci, '_run', failing)
    answer = ci.definitions(root, 'format_money', language='python')
    assert answer['outcome'] == ci.BACKEND_ERROR, answer
    assert answer['answered'] is False
    assert answer['complete'] is False
    assert 'database error' in answer['reason']
    relation = ci.callers(root, 'format_money', language='python')
    assert relation['answered'] is False


def test_a_timeout_and_unparseable_output_are_each_their_own_answer(tmp_path, monkeypatch):
    root = _repo(tmp_path, 'python')
    ci.ensure_indexed(root)
    real = ci._run

    def timing_out(index_root, args, **kwargs):
        if args[:1] == ['query']:
            return ci._Invocation(None, '', '', timed_out=True, error='超时')
        return real(index_root, args, **kwargs)

    monkeypatch.setattr(ci, '_run', timing_out)
    assert ci.definitions(root, 'x', language='python')['outcome'] == ci.BACKEND_TIMEOUT

    def garbling(index_root, args, **kwargs):
        if args[:1] == ['query']:
            return ci._Invocation(0, 'Segmentation fault, sorry', '')
        return real(index_root, args, **kwargs)

    monkeypatch.setattr(ci, '_run', garbling)
    garbled = ci.definitions(root, 'x', language='python')
    assert garbled['outcome'] == ci.MALFORMED_OUTPUT, garbled
    # Only the backend's own confirmed sentence may become no_match.
    def not_found(index_root, args, **kwargs):
        if args[:1] == ['query']:
            return ci._Invocation(0, 'ℹ Symbol "x" not found', '')
        return real(index_root, args, **kwargs)

    monkeypatch.setattr(ci, '_run', not_found)
    empty = ci.definitions(root, 'x', language='python')
    assert empty['outcome'] == ci.NO_MATCH and empty['complete'] is True


def test_a_full_page_before_filtering_cannot_claim_no_match_or_completeness(
        tmp_path, monkeypatch):
    """The review's probe: limit 2, three same-name Python nodes, asking for Java.

    The old code answered ``no_match`` with ``truncated=False``. It could not
    rule out a Java node sitting in the part of the page the backend never
    returned, so neither claim was supportable.
    """
    root = _repo(tmp_path, 'java')
    ci.ensure_indexed(root)
    real = ci._run

    def saturated(index_root, args, **kwargs):
        if args[:1] == ['query']:
            page = int(args[args.index('--limit') + 1])
            node = {'kind': 'function', 'name': 'format', 'qualifiedName': 'm.format',
                    'filePath': 'py/m.py', 'language': 'python', 'startLine': 1,
                    'id': 'function:1'}
            return ci._Invocation(0, json.dumps([{'node': node}] * page), '')
        return real(index_root, args, **kwargs)

    monkeypatch.setattr(ci, '_run', saturated)
    answer = ci.definitions(root, 'format', language='java', limit=2)
    assert answer['outcome'] == ci.INCOMPLETE, answer
    assert answer['complete'] is False
    assert answer['results'] == []
    # It still reports what it filtered out, and it does not say "no match".
    assert any(d['language'] == 'python' for d in answer['dropped_other_language'])
    assert '不能据此断定没有匹配' in answer['reason']


def test_two_same_language_definitions_of_one_name_are_not_silently_picked(tmp_path):
    """A name-keyed relation query does not pin the callee's identity.

    Two Java classes each declaring ``format`` are two different targets. The
    backend answers for whichever it matched; claiming that as *the* call
    relation would be precise-looking and unfounded.
    """
    root = tmp_path / 'ambiguous'
    root.mkdir()
    _git(root, 'init', '-q', '-b', 'main')
    _git(root, 'config', 'user.email', 't@example.com')
    _git(root, 'config', 'user.name', 'T')
    _write(root, 'src/a/Money.java', '''package a;

public class Money {
    public static String format(long c) { return ""; }
}
''')
    _write(root, 'src/b/Money.java', '''package b;

public class Money {
    public static String format(long c) { return ""; }
}
''')
    _write(root, 'src/a/Report.java', '''package a;

public class Report {
    public String render(long c) { return Money.format(c); }
}
''')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'base')

    answer = ci.callers(root, 'format', language='java')
    assert answer['outcome'] == ci.AMBIGUOUS, answer
    assert answer['results'] == []
    assert len(answer['candidates']) == 2
    assert 'target_path' in answer['reason']

    # Pinning the target by path resolves it, and the answer names what it
    # resolved to rather than leaving the reader to assume.
    pinned = ci.callers(root, 'format', language='java',
                        target_path='src/a/Money.java')
    assert pinned['outcome'] in (ci.OK, ci.NO_MATCH), pinned
    assert pinned['target']['path'] == 'src/a/Money.java'


def test_choosing_a_target_actually_constrains_which_edges_come_back(tmp_path):
    """The review's probe: ``save`` in ``a/`` and ``b/``, each with its own caller.

    Picking a target used to decorate the answer without changing the query --
    ``callers save`` was still what ran, so choosing ``a/store.py`` could return
    ``b/``'s caller. The relation query is now bound to the chosen definition,
    and this asserts the two targets give disjoint answers.
    """
    root = tmp_path / 'dup'
    root.mkdir()
    _git(root, 'init', '-q', '-b', 'main')
    _git(root, 'config', 'user.email', 't@example.com')
    _git(root, 'config', 'user.name', 'T')
    _write(root, 'a/__init__.py', '')
    _write(root, 'b/__init__.py', '')
    _write(root, 'a/store.py', 'def save(row):\n    return "a:" + str(row)\n')
    _write(root, 'b/store.py', 'def save(row):\n    return "b:" + str(row)\n')
    _write(root, 'a/caller_a.py',
           'from a.store import save\n\n\ndef calls_a(row):\n    return save(row)\n')
    _write(root, 'b/caller_b.py',
           'from b.store import save\n\n\ndef calls_b(row):\n    return save(row)\n')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'base')

    # Without a target there are two candidates, so no precise answer is given.
    undecided = ci.callers(root, 'save', language='python')
    assert undecided['outcome'] == ci.AMBIGUOUS, undecided
    assert {c['path'] for c in undecided['candidates']} == {'a/store.py', 'b/store.py'}

    a_side = ci.callers(root, 'save', language='python', target_path='a/store.py')
    assert a_side['outcome'] == ci.OK, a_side
    assert a_side['target']['path'] == 'a/store.py'
    a_callers = {r['path'] for r in a_side['results']}
    assert 'a/caller_a.py' in a_callers
    assert not any(p.startswith('b/') for p in a_callers), a_side

    b_side = ci.callers(root, 'save', language='python', target_path='b/store.py')
    assert b_side['outcome'] == ci.OK, b_side
    assert b_side['target']['path'] == 'b/store.py'
    b_callers = {r['path'] for r in b_side['results']}
    assert 'b/caller_b.py' in b_callers
    assert not any(p.startswith('a/') for p in b_callers), b_side

    assert a_callers.isdisjoint(b_callers)


def test_a_candidate_set_that_cannot_be_proven_complete_is_not_a_unique_target(
        tmp_path, monkeypatch):
    """One candidate out of an unknown whole is not one candidate.

    If the definition page filled up before filtering, the definition we did not
    see could be the real callee -- so no precise relation is returned, even
    though exactly one candidate came back.
    """
    root = _repo(tmp_path, 'python')
    ci.ensure_indexed(root)
    real = ci._run

    def saturated(index_root, args, **kwargs):
        if args[:1] == ['query']:
            page = int(args[args.index('--limit') + 1])
            node = {'kind': 'function', 'name': 'format_money',
                    'qualifiedName': 'acme.money.format_money',
                    'filePath': 'acme/money.py', 'language': 'python',
                    'startLine': 1, 'id': 'function:1'}
            filler = {**node, 'language': 'go', 'filePath': 'x/y.go'}
            return ci._Invocation(
                0, json.dumps([{'node': node}] + [{'node': filler}] * (page - 1)), '')
        return real(index_root, args, **kwargs)

    monkeypatch.setattr(ci, '_run', saturated)
    answer = ci.callers(root, 'format_money', language='python')
    assert answer['outcome'] == ci.AMBIGUOUS, answer
    assert answer['complete'] is False
    assert answer['results'] == []
    assert '无法证明目标唯一' in answer['reason']
