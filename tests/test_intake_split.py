"""需求拆分测试。fake binary，不调真模型。

这一层的两个危险方向，都朝「看起来更能干」走：

  1. 硬切。一件事被切成三步 → 三个任务，每个单独跑一遍单独花钱，而且互相
     依赖着串行。所以「只有一件事时退化成一条」必须有测试。
  2. 静默丢依赖边。指向不存在 slug 的边被悄悄丢掉，任务照样入队，然后在
     半小时后以「等不到前置」的形式冒出来 —— 那时已经离拆分很远了。
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from factory.intake.extract import IntakeError
from factory.intake.split import (SubTask, TaskSplitter, _normalize,
                                  _reject_cycles, _slugify)


def _fake(tmp_path: Path, payload: dict, name="fake_claude") -> str:
    script = tmp_path / name
    script.write_text(
        f"#!/bin/sh\ncat <<'__END__'\n{json.dumps(payload)}\n__END__\n",
        encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _wrapped(subtasks: list, *, cost=0.0002) -> dict:
    return {"is_error": False,
            "structured_output": {"subtasks": subtasks},
            "usage": {"input_tokens": 30, "output_tokens": 40},
            "total_cost_usd": cost}


def _split(tmp_path: Path, subtasks: list, **kw):
    return TaskSplitter(binary=_fake(tmp_path, _wrapped(subtasks, **kw))).run("需求")


# ---------- 退化 ----------

def test_one_subtask_is_not_a_split(tmp_path: Path) -> None:
    """一条 = 退化成原来的行为。

    `split` 为 True 的话，调用方会走「多条」那条路：多写几份 YAML、多花几份
    钱，而用户只说了一件事。
    """
    r = _split(tmp_path, [{"slug": "add-retry", "description": "加重试"}])
    assert len(r.subtasks) == 1
    assert r.split is False


def test_no_subtasks_degrades_instead_of_raising(tmp_path: Path) -> None:
    """拆分器什么都没返回 → 当一件事处理，并把原话留住。

    抛的话，一句本来就不该拆的需求会因为「开了 --split」而整个失败。
    """
    r = TaskSplitter(binary=_fake(
        tmp_path, {"is_error": False, "structured_output": {"subtasks": []},
                   "usage": {}, "total_cost_usd": 0.0})).run("就一句话")
    assert len(r.subtasks) == 1
    assert r.subtasks[0].description == "就一句话"
    assert r.dropped and "按一条处理" in r.dropped[0]


def test_split_flag_is_true_for_two(tmp_path: Path) -> None:
    r = _split(tmp_path, [{"slug": "a", "description": "甲"},
                          {"slug": "b", "description": "乙"}])
    assert r.split is True
    assert [s.slug for s in r.subtasks] == ["a", "b"]
    assert r.cost_usd > 0 and r.tokens == 70


# ---------- 依赖边 ----------

def test_dependency_edge_survives(tmp_path: Path) -> None:
    r = _split(tmp_path, [
        {"slug": "a", "description": "甲"},
        {"slug": "b", "description": "乙", "depends_on": ["a"]},
    ])
    assert r.subtasks[1].depends_on == ("a",)


def test_unknown_slug_edge_is_dropped_loudly(tmp_path: Path) -> None:
    """不静默丢。

    留着它 = 队列层的死锁，而那个错误在半小时后才以「等不到前置」冒出来。
    悄悄丢掉 = 依赖不存在了，两件本该串行的事并行跑。两个方向都得说出来。
    """
    r = _split(tmp_path, [
        {"slug": "a", "description": "甲"},
        {"slug": "b", "description": "乙", "depends_on": ["a", "ghost"]},
    ])
    assert r.subtasks[1].depends_on == ("a",)
    assert any("ghost" in d for d in r.dropped)


def test_self_dependency_is_dropped_loudly(tmp_path: Path) -> None:
    r = _split(tmp_path, [{"slug": "a", "description": "甲",
                           "depends_on": ["a"]}])
    assert r.subtasks[0].depends_on == ()
    assert any("依赖自己" in d for d in r.dropped)


def test_cycle_raises_at_intake(tmp_path: Path) -> None:
    """环在这里就抛。

    队列层也能抓到（deadlocked()），但那要等到入队之后、人已经走开之后。
    在这里抛，人还在终端前面。
    """
    with pytest.raises(IntakeError) as exc:
        _split(tmp_path, [
            {"slug": "a", "description": "甲", "depends_on": ["b"]},
            {"slug": "b", "description": "乙", "depends_on": ["a"]},
        ])
    assert "循环依赖" in str(exc.value)
    assert "a" in str(exc.value) and "b" in str(exc.value)


def test_three_way_cycle_raises(tmp_path: Path) -> None:
    with pytest.raises(IntakeError):
        _split(tmp_path, [
            {"slug": "a", "description": "甲", "depends_on": ["b"]},
            {"slug": "b", "description": "乙", "depends_on": ["c"]},
            {"slug": "c", "description": "丙", "depends_on": ["a"]},
        ])


def test_chain_is_not_a_cycle(tmp_path: Path) -> None:
    """非空基线：真链条不许被环检测误杀。

    只测「环报错」的话，一个「凡有依赖就报错」的实现也是绿的。
    """
    r = _split(tmp_path, [
        {"slug": "a", "description": "甲"},
        {"slug": "b", "description": "乙", "depends_on": ["a"]},
        {"slug": "c", "description": "丙", "depends_on": ["b"]},
    ])
    assert r.subtasks[2].depends_on == ("b",)


# ---------- 清洗 ----------

def test_slugify_strips_what_cannot_be_a_filename() -> None:
    """slug 会变成依赖键，带空格的话依赖永远对不上。"""
    assert _slugify("Add Retry") == "add-retry"
    assert _slugify("  --Fix   Timeout!! ") == "fix-timeout"
    assert _slugify("") == ""


def test_duplicate_slug_is_renamed_not_overwritten() -> None:
    """撞名不覆盖。

    覆盖会让先到的那条静默消失，而别人的依赖边还指着这个 slug ——
    一条真依赖会悄悄挂到另一件事上。
    """
    subs, dropped = _normalize([
        {"slug": "a", "description": "甲"},
        {"slug": "a", "description": "另一个甲"},
    ])
    assert [s.slug for s in subs] == ["a", "a-2"]
    assert [s.description for s in subs] == ["甲", "另一个甲"]
    assert any("重复" in d for d in dropped)


def test_empty_description_is_dropped_loudly() -> None:
    """描述为空的子任务留不住 —— 它会被单独递给抽取器，那边只会失败。"""
    subs, dropped = _normalize([
        {"slug": "a", "description": "甲"},
        {"slug": "b", "description": "   "},
    ])
    assert [s.slug for s in subs] == ["a"]
    assert any("描述为空" in d for d in dropped)


def test_missing_slug_gets_a_placeholder() -> None:
    subs, _ = _normalize([{"description": "甲"}])
    assert subs[0].slug == "sub-1"


# ---------- 调用失败 ----------

def test_error_payload_raises(tmp_path: Path) -> None:
    binary = _fake(tmp_path, {"is_error": True, "subtype": "overloaded",
                              "stop_reason": "", "result": "服务忙"})
    with pytest.raises(IntakeError) as exc:
        TaskSplitter(binary=binary).run("需求")
    assert "拆分调用失败" in str(exc.value)


def test_non_json_raises(tmp_path: Path) -> None:
    script = tmp_path / "junk"
    script.write_text("#!/bin/sh\necho 不是 JSON\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(IntakeError) as exc:
        TaskSplitter(binary=str(script)).run("需求")
    assert "非 JSON" in str(exc.value)


def test_missing_binary_raises(tmp_path: Path) -> None:
    with pytest.raises(IntakeError) as exc:
        TaskSplitter(binary=str(tmp_path / "nope")).run("需求")
    assert "无法启动" in str(exc.value)


def test_splitter_gets_no_tools(tmp_path: Path) -> None:
    """关工具在这里防的是「替用户增加需求」。

    能读仓库的拆分器会看见「这里还该改一处」，然后切出一件用户没要求的事，
    而它下游每一条都会真的花钱改代码。
    """
    argv = TaskSplitter(binary="claude")._argv("p")
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    assert "--safe-mode" in argv


# ---------- CLI 接线 ----------

def _ns(**kw):
    import argparse
    base = dict(text="甲和乙", text_file=None, audio=None, whisper_binary="whisper",
                whisper_model="small", language=None, binary="claude",
                intake_model="sonnet", split=False, split_model="haiku",
                dry_run=True, propose_checks=False, workspace=None,
                queue=None, output=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_slug_edges_become_real_task_ids(tmp_path: Path, monkeypatch) -> None:
    """slug → task_id 的翻译在 CLI 层做，且用抽取器给的 id。

    自己拼 slug 当 id 的话，YAML 里的 task_id 和依赖里写的名字对不上，
    而依赖对不上的表现是**任务永远等下去** —— 不是报错。
    """
    from factory.cli import _split_and_extract
    from factory.intake.extract import DraftTask

    class FakeSplitter:
        def __init__(self, **_kw) -> None:
            pass

        def run(self, _desc):
            from factory.intake.split import SplitResult
            return SplitResult(subtasks=(
                SubTask(slug="a", description="甲"),
                SubTask(slug="b", description="乙", depends_on=("a",)),
            ))

    monkeypatch.setattr("factory.intake.split.TaskSplitter", FakeSplitter)

    seen: list[str] = []

    class FakeExtractor:
        def run(self, desc):
            seen.append(desc)
            return DraftTask(task_id=f"T-{len(seen)}", prompt=desc)

    drafts = _split_and_extract("甲和乙", FakeExtractor(), _ns(split=True))
    assert seen == ["甲", "乙"]
    assert [d.task_id for d in drafts] == ["T-1", "T-2"]
    # 依赖指向的是抽取器给的真 id，不是 slug。
    assert drafts[0].depends_on == ()
    assert drafts[1].depends_on == ("T-1",)


def test_single_subtask_goes_through_the_old_path(tmp_path: Path,
                                                  monkeypatch) -> None:
    """只有一件事时只抽取一次，且抽的是原描述。"""
    from factory.cli import _split_and_extract
    from factory.intake.extract import DraftTask
    from factory.intake.split import SplitResult

    class FakeSplitter:
        def __init__(self, **_kw) -> None:
            pass

        def run(self, _desc):
            return SplitResult(subtasks=(SubTask(slug="a", description="就一件"),))

    monkeypatch.setattr("factory.intake.split.TaskSplitter", FakeSplitter)
    calls: list[str] = []

    class FakeExtractor:
        def run(self, desc):
            calls.append(desc)
            return DraftTask(task_id="T-1", prompt=desc)

    drafts = _split_and_extract("就一件", FakeExtractor(), _ns(split=True))
    assert len(drafts) == 1 and len(calls) == 1


def test_depends_on_lands_in_the_yaml(tmp_path: Path) -> None:
    """草稿的 depends_on 必须真写进 YAML，且能被 Task.from_yaml 读回来。

    这条测的是接线：一个只填了 dataclass 字段、to_yaml 里漏掉的实现，
    在别处看起来完全正常。
    """
    import yaml as _yaml
    from factory.intake.extract import DraftTask
    from factory.task import Task

    p = DraftTask(task_id="T-b", prompt="乙", depends_on=("T-a",)).write(
        tmp_path / "T-b.yaml")
    assert _yaml.safe_load(p.read_text("utf-8"))["depends_on"] == ["T-a"]
    assert Task.from_yaml(p).depends_on == ("T-a",)


def test_no_depends_on_key_when_empty(tmp_path: Path) -> None:
    """没前置就不写这个键：每份手写 YAML 少一行噪音。"""
    import yaml as _yaml
    from factory.intake.extract import DraftTask
    p = DraftTask(task_id="T-a", prompt="甲").write(tmp_path / "T-a.yaml")
    assert "depends_on" not in _yaml.safe_load(p.read_text("utf-8"))


# ---------- 撞名 ----------

def test_id_clash_with_deps_refuses_to_write_anything(tmp_path: Path) -> None:
    """撞名 + 有依赖边 → 一条都不写。

    `_admit_to_queue` 撞名时改成 `T-x.2.yaml`，而队列层判依赖满足要剥掉
    `.N` 后缀（不剥的话跑第二遍的前置永远满足不了后继）。两条规矩合起来：
    新入队的 `T-x.2` 的后继会在**老的** `T-x` 合并时就解锁 —— 一个没跑的
    前置被当成跑过了，而这在任何一处日志里都看不出来。
    """
    from factory.backlog.store import Backlog, INBOX
    from factory.cli import _dep_id_clash
    from factory.intake.extract import DraftTask

    bl = Backlog(tmp_path / "q").ensure()
    (bl.dir(INBOX) / "T-a.yaml").write_text("task_id: T-a\nprompt: x\n",
                                            encoding="utf-8")
    drafts = [DraftTask(task_id="T-a", prompt="甲"),
              DraftTask(task_id="T-b", prompt="乙", depends_on=("T-a",))]
    msg = _dep_id_clash(drafts, _ns(queue=str(tmp_path / "q")))
    assert msg and "T-a" in msg and "inbox" in msg


def test_id_clash_in_done_also_refuses(tmp_path: Path) -> None:
    """done/ 里的同名最危险：它已经「合并」了，后继会立刻被解锁。"""
    from factory.backlog.store import Backlog, DONE
    from factory.cli import _dep_id_clash
    from factory.intake.extract import DraftTask

    bl = Backlog(tmp_path / "q").ensure()
    (bl.dir(DONE) / "T-a.yaml").write_text("task_id: T-a\nprompt: x\n",
                                           encoding="utf-8")
    drafts = [DraftTask(task_id="T-a", prompt="甲"),
              DraftTask(task_id="T-b", prompt="乙", depends_on=("T-a",))]
    assert "done" in _dep_id_clash(drafts, _ns(queue=str(tmp_path / "q")))


def test_no_clash_no_complaint(tmp_path: Path) -> None:
    """非空基线：队列里有别的条目，但不撞名 → 放行。

    只测「撞名被拦」的话，一个「有依赖就一律拒绝」的实现也是绿的。
    """
    from factory.backlog.store import Backlog, INBOX
    from factory.cli import _dep_id_clash
    from factory.intake.extract import DraftTask

    bl = Backlog(tmp_path / "q").ensure()
    (bl.dir(INBOX) / "T-other.yaml").write_text("task_id: T-other\nprompt: x\n",
                                                encoding="utf-8")
    drafts = [DraftTask(task_id="T-a", prompt="甲"),
              DraftTask(task_id="T-b", prompt="乙", depends_on=("T-a",))]
    assert _dep_id_clash(drafts, _ns(queue=str(tmp_path / "q"))) == ""


def test_clash_without_deps_keeps_the_old_suffix_behaviour(tmp_path: Path
                                                          ) -> None:
    """没有依赖边时撞名是老行为（加后缀），不动它。

    收紧到「任何撞名都拒」会把一个一直能用的用法（同一需求重跑一遍）改掉，
    而那条路上没有依赖，后缀是安全的。
    """
    from factory.backlog.store import Backlog, INBOX
    from factory.cli import _dep_id_clash
    from factory.intake.extract import DraftTask

    bl = Backlog(tmp_path / "q").ensure()
    (bl.dir(INBOX) / "T-a.yaml").write_text("task_id: T-a\nprompt: x\n",
                                            encoding="utf-8")
    drafts = [DraftTask(task_id="T-a", prompt="甲"),
              DraftTask(task_id="T-b", prompt="乙")]
    assert _dep_id_clash(drafts, _ns(queue=str(tmp_path / "q"))) == ""


def test_cmd_prd_actually_calls_the_clash_guard(tmp_path: Path,
                                                monkeypatch) -> None:
    """接线测试，不只测 _dep_id_clash 本身。

    一道没接上的闸门和一道接上了的，在单元测试和报表上长得完全一样 ——
    第一版变异跑出来就是这条存活的。判据：撞名时**一个文件都没落盘**。
    """
    from factory import cli
    from factory.backlog.store import Backlog, INBOX
    from factory.intake.extract import DraftTask
    from factory.intake.split import SplitResult

    bl = Backlog(tmp_path / "q").ensure()
    (bl.dir(INBOX) / "T-a.yaml").write_text("task_id: T-a\nprompt: x\n",
                                            encoding="utf-8")
    before = sorted(p.name for p in bl.dir(INBOX).iterdir())

    class FakeSplitter:
        def __init__(self, **_kw) -> None:
            pass

        def run(self, _desc):
            return SplitResult(subtasks=(
                SubTask(slug="a", description="甲"),
                SubTask(slug="b", description="乙", depends_on=("a",))))

    ids = iter(["T-a", "T-b"])

    class FakeExtractor:
        def __init__(self, **_kw) -> None:
            pass

        def run(self, desc):
            return DraftTask(task_id=next(ids), prompt=desc)

    monkeypatch.setattr("factory.intake.split.TaskSplitter", FakeSplitter)
    monkeypatch.setattr(cli, "TaskExtractor", FakeExtractor)

    code = cli._cmd_prd(_ns(split=True, dry_run=False,
                            queue=str(tmp_path / "q")))
    assert code == 2
    # 非空基线：inbox 里本来就有一个条目，所以「没新增」不等于「目录是空的」。
    assert sorted(p.name for p in bl.dir(INBOX).iterdir()) == before
    assert list(bl.dir("needs-human").iterdir()) == []
