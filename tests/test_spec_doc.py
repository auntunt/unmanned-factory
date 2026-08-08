"""从规格文档里按编号查正文。

**为什么是确定性解析，不是让模型抽。** 抽取会出错，而抽错的标准比没有标准
更危险：监工会拿着一条不存在的要求去判 diff，判得理直气壮，没有任何下游
能发现那条标准是编出来的 —— 它长得和真标准一模一样。

所以这里只认 Markdown 的三种行首形状，认不出就报「查不到」，
交给上游拦（闸门 / dispatcher）。宁可拒，不可编。
"""

from __future__ import annotations

from factory.spec_doc import find_in_text, resolve

DOC = """\
# 需求文档

## 验收标准

- AC-1: 登录接口须满足
  密码错误返回 401
  锁定后返回 423
- AC-2: 密码不入日志

### AC-3 会话超时 30 分钟

AC-4. 支持记住我

- AC-5:
- AC-6: 有正文

## 下一节

正文里提到 AC-2 但这不是定义行。
"""


# ---------- 三种行首形状 ----------

def test_a_bullet_ref_is_found():
    assert find_in_text(DOC, "AC-2") == "AC-2: 密码不入日志"


def test_a_heading_ref_is_found():
    assert find_in_text(DOC, "AC-3") == "AC-3: 会话超时 30 分钟"


def test_a_bare_numbered_line_is_found():
    assert find_in_text(DOC, "AC-4") == "AC-4: 支持记住我"


def test_the_separator_is_normalised_to_a_colon():
    """原文用 `.`、空格、还是全角冒号，出来都是 `编号: 正文`。

    统一形状是有意的：递给规格监工的是一串标准，三种写法混在一起会让
    「这是编号」这件事变成模型要猜的。三种行首形状是**文档作者**的自由，
    不该变成监工的输入噪声。
    """
    for line in ("AC-7. 正文", "### AC-7 正文", "- AC-7：正文"):
        assert find_in_text(line + "\n", "AC-7") == "AC-7: 正文"


def test_the_body_keeps_the_ref_so_a_human_can_trace_it_back():
    """带编号是有意的：审计记录里的标准要能对回 PRD 的哪一条。

    剥掉编号的话，claim 里写着「密码不入日志」，而人要在 PRD 里搜这句话 ——
    措辞一改就对不上了。编号是那个稳定的锚。
    """
    assert find_in_text(DOC, "AC-2").startswith("AC-2")


# ---------- 多行 ----------

def test_indented_continuation_lines_are_joined():
    assert find_in_text(DOC, "AC-1") == \
        "AC-1: 登录接口须满足 密码错误返回 401 锁定后返回 423"


def test_a_continuation_stops_at_the_next_ref():
    """AC-1 的续行不能吞掉 AC-2。"""
    assert "密码不入日志" not in find_in_text(DOC, "AC-1")


def test_a_continuation_stops_at_a_new_section():
    assert "下一节" not in find_in_text(DOC, "AC-6")


# ---------- 查不到 ----------

def test_an_undefined_ref_is_not_found():
    assert find_in_text(DOC, "AC-99") is None


def test_a_ref_mentioned_only_in_prose_is_not_a_definition():
    """最后一节提到 AC-2，但那是引用不是定义。

    行首匹配就是为了区分这两件事：满篇 grep 编号会把随口一提当成标准。
    """
    prose = "实现时注意 AC-2 的要求。\n"
    assert find_in_text(prose, "AC-2") is None


def test_a_ref_with_an_empty_body_is_the_same_as_not_found():
    """`- AC-5:` 后面什么都没有 —— 命中了行首，但没有可核对的东西。"""
    assert find_in_text(DOC, "AC-5") is None


def test_only_the_first_definition_wins():
    """同一个编号定义两次：取第一条，不拼起来也不报错。

    重复定义是文档自己的毛病，这里不替它决定哪条算 —— 但也不能因此
    整个任务卡住，那会让一份有瑕疵的 PRD 变成派发不了。
    """
    dup = "- AC-1: 第一条\n- AC-1: 第二条\n"
    assert find_in_text(dup, "AC-1") == "AC-1: 第一条"


# ---------- resolve：路径、错误、上限 ----------

def test_resolve_reads_the_doc_relative_to_the_workspace(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "prd.md").write_text("- AC-1: 正文\n", encoding="utf-8")
    got = resolve(("AC-1",), "docs/prd.md", root=tmp_path)
    assert got.ok
    assert got.bodies == ("AC-1: 正文",)


def test_resolve_reports_every_missing_ref_at_once(tmp_path):
    """一次报全，不是撞到第一个就返回。

    人补文档时要一次看到少哪几条，否则改一条派发一次、烧一次钱才知道下一条。
    """
    (tmp_path / "prd.md").write_text("- AC-1: 有\n", encoding="utf-8")
    got = resolve(("AC-1", "AC-2", "AC-3"), "prd.md", root=tmp_path)
    assert not got.ok
    assert got.missing == ("AC-2", "AC-3")
    assert got.bodies == ("AC-1: 有",), "查到的那条照样带回来"


def test_refs_without_a_doc_is_a_doc_error_not_a_missing_ref(tmp_path):
    """区分「没给文档」和「文档里没这条」：人要做的事不一样。

    前者加一行 spec_doc，后者去改文档或改编号。合成一个错误会让提示词
    答不出该干哪件。
    """
    got = resolve(("AC-1",), None, root=tmp_path)
    assert not got.ok
    assert "没有 spec_doc" in got.doc_error
    assert got.missing == ()


def test_a_missing_file_is_a_doc_error(tmp_path):
    got = resolve(("AC-1",), "nope.md", root=tmp_path)
    assert not got.ok
    assert "不存在" in got.doc_error


def test_no_refs_resolves_clean_even_without_a_doc(tmp_path):
    """口述任务的形状：没有编号，也就没什么要查的。"""
    got = resolve((), None, root=tmp_path)
    assert got.ok
    assert got.bodies == ()


def test_an_oversized_doc_is_refused_rather_than_read_whole(tmp_path):
    """上限存在的理由：spec_doc 是人填的路径，指错到一个日志文件不该
    把几百 MB 读进内存再塞进监工提示词。"""
    from factory.spec_doc import _MAX_DOC
    (tmp_path / "big.md").write_text("x" * (_MAX_DOC + 1), encoding="utf-8")
    got = resolve(("AC-1",), "big.md", root=tmp_path)
    assert not got.ok
    assert got.doc_error


def test_a_runaway_body_is_truncated(tmp_path):
    """一条标准长到几千字通常是文档结构没被认出来（续行吞了整节）。
    截断而不是原样递：监工提示词里塞进半篇文档会把真标准淹掉。"""
    from factory.spec_doc import _MAX_BODY
    body = "很长" * _MAX_BODY
    (tmp_path / "prd.md").write_text(f"- AC-1: {body}\n", encoding="utf-8")
    got = resolve(("AC-1",), "prd.md", root=tmp_path)
    assert got.ok
    assert len(got.bodies[0]) <= _MAX_BODY + 40
