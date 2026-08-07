import pytest

from factory.audit.models import OracleClass, Resolution, SupervisorRole, Verdict
from factory.audit.store import AuditStore


@pytest.fixture
def store():
    return AuditStore(":memory:")


def _open(store, task_id="T-1", oracle_class=OracleClass.A):
    return store.open_attempt(
        task_id=task_id, spec_ref=["AC-1"],
        oracle_class=oracle_class, class_reason="no rule matched -> default A",
        harness="claude_code", harness_version="2.1.223", model="haiku",
    )


def test_next_attempt_no_starts_at_one(store):
    assert store.next_attempt_no("T-1") == 1


def test_attempt_no_increments_per_task(store):
    _open(store, "T-1")
    _open(store, "T-1")
    _open(store, "T-2")
    assert store.next_attempt_no("T-1") == 3
    assert store.next_attempt_no("T-2") == 2


def test_open_attempt_persists_all_fields(store):
    aid = _open(store)
    row = store.get(aid)
    assert row.task_id == "T-1"
    assert row.attempt_no == 1
    assert row.spec_ref == ["AC-1"]
    assert row.oracle_class == OracleClass.A
    assert row.harness == "claude_code"
    assert row.model == "haiku"
    assert row.resolution == Resolution.PENDING


def test_record_result_then_verdict_then_finalize(store):
    aid = _open(store)
    store.record_result(
        aid, diff_hash="abc123", commit="deadbeef",
        transcript_path="/tmp/x.jsonl",
        tokens_in=100, tokens_out=50, cost_usd=0.0021, wall_clock_ms=4200,
    )
    store.record_verdict(
        aid, role=SupervisorRole.REGRESSION, verdict=Verdict.PASS, claims=[],
    )
    store.finalize(aid, Resolution.MERGED)

    row = store.get(aid)
    assert row.diff_hash == "abc123"
    assert row.commit == "deadbeef"
    assert row.transcript_path == "/tmp/x.jsonl"
    assert (row.tokens_in, row.tokens_out) == (100, 50)
    assert row.cost_usd == pytest.approx(0.0021)
    assert row.wall_clock_ms == 4200
    assert row.resolution == Resolution.MERGED
    assert len(row.supervisors) == 1
    assert row.supervisors[0].role == SupervisorRole.REGRESSION


def test_record_result_can_overwrite_harness_version(store):
    """open_attempt 时 harness 版本可能还没探到，跑完才知道 —— 允许覆盖写。"""
    aid = _open(store)
    store.record_result(
        aid, diff_hash=None, commit=None, transcript_path=None,
        tokens_in=0, tokens_out=0, cost_usd=0.0, wall_clock_ms=1,
        harness_version="2.1.999",
    )
    assert store.get(aid).harness_version == "2.1.999"


def test_record_result_keeps_harness_version_when_not_given(store):
    aid = _open(store)
    store.record_result(
        aid, diff_hash=None, commit=None, transcript_path=None,
        tokens_in=0, tokens_out=0, cost_usd=0.0, wall_clock_ms=1,
    )
    assert store.get(aid).harness_version == "2.1.223"


def test_multiple_verdicts_accumulate(store):
    aid = _open(store)
    store.record_verdict(aid, role=SupervisorRole.REGRESSION,
                         verdict=Verdict.FAIL, claims=[{"check": "pytest"}])
    store.record_verdict(aid, role=SupervisorRole.RISK,
                         verdict=Verdict.PASS, claims=[])
    roles = {v.role for v in store.get(aid).supervisors}
    assert roles == {SupervisorRole.REGRESSION, SupervisorRole.RISK}


def test_escalate_class_overwrites_class_and_reason(store):
    aid = _open(store, oracle_class=OracleClass.A)
    store.escalate_class(aid, oracle_class=OracleClass.D,
                         class_reason="post-diff: touched migrations/001.py")
    row = store.get(aid)
    assert row.oracle_class == OracleClass.D
    assert "migrations/001.py" in row.class_reason


def test_link_defect_appends(store):
    aid = _open(store)
    store.link_defect(aid, "BUG-1")
    store.link_defect(aid, "BUG-2")
    assert store.get(aid).linked_defects == ["BUG-1", "BUG-2"]


def test_attempts_for_is_ordered_and_scoped_to_task(store):
    _open(store, "T-1")
    _open(store, "T-2")
    _open(store, "T-1")
    rows = store.attempts_for("T-1")
    assert [r.attempt_no for r in rows] == [1, 2]
    assert {r.task_id for r in rows} == {"T-1"}
    assert store.attempts_for("T-nope") == ()


def test_attempts_for_loads_supervisors(store):
    aid = _open(store)
    store.record_verdict(aid, role=SupervisorRole.RISK,
                         verdict=Verdict.PASS, claims=[])
    (row,) = store.attempts_for("T-1")
    assert [v.role for v in row.supervisors] == [SupervisorRole.RISK]


def test_persists_across_store_instances(tmp_path):
    db = tmp_path / "audit.db"
    aid = _open(AuditStore(db))
    reopened = AuditStore(db)
    assert reopened.get(aid).task_id == "T-1"
    assert reopened.next_attempt_no("T-1") == 2


# ---------- 落库脱敏：Global Constraint 11 ----------

def test_claims_are_redacted_before_they_hit_the_db(store):
    """监工 claims 携带明文密钥时，落库必须已脱敏。"""
    aid = store.open_attempt(
        task_id="T-sec", spec_ref=[], oracle_class=OracleClass.A,
        class_reason="A", harness="claude_code", harness_version="x",
        model="haiku",
    )
    store.record_verdict(
        aid, role=SupervisorRole.REGRESSION, verdict=Verdict.FAIL,
        claims=[{
            "check": "deploy",
            "command": "sshpass -p Hunter2!x ssh root@8.8.8.8",
            "expected": "exit_zero",
            "got": "exit 1: api_key=abc123xyz789",
        }],
    )
    claims = store.get(aid).supervisors[0].claims
    blob = str(claims)
    assert "Hunter2!x" not in blob
    assert "abc123xyz789" not in blob
    # key 名要留着，否则审计看不出这里本来有个密钥
    assert "sshpass" in blob and "api_key" in blob
    assert claims[0]["check"] == "deploy"


def test_class_reason_is_redacted(store):
    aid = store.open_attempt(
        task_id="T-sec2", spec_ref=[], oracle_class=OracleClass.D,
        class_reason="D: matched password=Hunter2!x", harness="h",
        harness_version="x", model="opus",
    )
    assert "Hunter2!x" not in store.get(aid).class_reason
    store.escalate_class(aid, oracle_class=OracleClass.D,
                         class_reason="D: token=abc123xyz789")
    assert "abc123xyz789" not in store.get(aid).class_reason


def test_no_plaintext_secret_anywhere_in_the_db_file(tmp_path):
    """整库扫描：P0 判据「审计库里没有任何明文密码或 API key」。"""
    db = tmp_path / "audit.db"
    s = AuditStore(db)
    aid = s.open_attempt(
        task_id="T-scan", spec_ref=["AC-1"], oracle_class=OracleClass.A,
        class_reason="A: password=Hunter2!x", harness="claude_code",
        harness_version="2.1", model="haiku",
    )
    s.record_result(aid, diff_hash="d" * 64, commit="c" * 40,
                    transcript_path="/tmp/x.jsonl", tokens_in=1,
                    tokens_out=1, cost_usd=0.1, wall_clock_ms=5)
    s.record_verdict(aid, role=SupervisorRole.RISK, verdict=Verdict.FAIL,
                     claims=[{"got": "Authorization: Bearer eyJsecrettoken"}])
    s.finalize(aid, Resolution.ESCALATED)

    blob = db.read_bytes()
    for secret in (b"Hunter2!x", b"eyJsecrettoken"):
        assert secret not in blob, f"明文泄漏进库文件: {secret!r}"


# ---------- commit 回查（spec §5 漏报路径的中间一跳） ----------

def test_record_commit_backfills_the_field(store):
    aid = _open(store)
    assert store.get(aid).commit is None
    store.record_commit(aid, "b" * 40)
    assert store.get(aid).commit == "b" * 40


def test_an_ambiguous_short_sha_returns_none_rather_than_a_guess(store):
    """前缀撞车时不许随便挑一条。

    挂错 attempt 的 defect 会把漏报记到无关的监工头上，比查不到更糟 ——
    查不到人会再试，挂错了没人知道，而 spec §5.1 的漏报数是裁剪监工的判据。
    """
    a, b = _open(store, "T-1"), _open(store, "T-2")
    store.record_commit(a, "abc111" + "0" * 34)
    store.record_commit(b, "abc222" + "0" * 34)

    assert store.attempt_by_commit("abc") is None, "撞车必须返回 None"
    # 前缀长到能区分就正常查到
    assert store.attempt_by_commit("abc111").task_id == "T-1"
    assert store.attempt_by_commit("abc222").task_id == "T-2"


def test_attempts_with_no_commit_are_never_matched(store):
    # commit 为 None 的行不该被任何前缀命中 —— 否则一个空 sha 会匹配到
    # 所有还没落地的 attempt。
    _open(store)
    assert store.attempt_by_commit("a") is None
