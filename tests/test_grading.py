import pytest

from factory.audit.models import OracleClass
from factory.grading.rules import DEFAULT_REASON, Grade, GradingEngine, Rule


def test_no_match_defaults_to_a():
    g = GradingEngine([]).grade(["README.md"])
    assert g.oracle_class == OracleClass.A
    assert g.reason == DEFAULT_REASON
    assert g.unmanned_allowed is True
    assert g.hard_gate is False


def test_fnmatch_star_crosses_slashes():
    engine = GradingEngine([
        Rule(OracleClass.D, "schema migration", patterns=("*migrations/*",)),
    ])
    assert engine.grade(["src/app/migrations/001.py"]).oracle_class == OracleClass.D
    assert engine.grade(["migrations/001.py"]).oracle_class == OracleClass.D
    assert engine.grade(["src/models.py"]).oracle_class == OracleClass.A


def test_most_severe_rule_wins_regardless_of_order():
    engine = GradingEngine([
        Rule(OracleClass.D, "migration", patterns=("*migrations/*",)),
        Rule(OracleClass.B, "api surface", patterns=("*api/*",)),
    ])
    g = engine.grade(["src/api/v1.py", "db/migrations/003.py"])
    assert g.oracle_class == OracleClass.D
    assert "migration" in g.reason


def test_reason_carries_triggering_paths_for_traceability():
    engine = GradingEngine([Rule(OracleClass.C, "auth", patterns=("*auth/*",))])
    g = engine.grade(["src/auth/login.py"])
    assert g.triggers == ("src/auth/login.py",)
    assert "src/auth/login.py" in g.reason


def test_ops_match_without_any_file_change():
    """生产部署改动 0 个文件，也必须判成 D。"""
    engine = GradingEngine([
        Rule(OracleClass.D, "irreversible op", ops=("prod_deploy",)),
    ])
    g = engine.grade(paths=[], ops=["prod_deploy"])
    assert g.oracle_class == OracleClass.D
    assert g.hard_gate is True


def test_grade_comparison():
    a = Grade(OracleClass.A, "x")
    d = Grade(OracleClass.D, "y")
    assert d.more_severe_than(a) is True
    assert a.more_severe_than(d) is False
    assert a.more_severe_than(Grade(OracleClass.A, "z")) is False


def test_from_yaml(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(
        "rules:\n"
        "  - class: D\n"
        "    reason: data deletion\n"
        "    ops: [data_delete]\n"
        "  - class: C\n"
        "    reason: crypto\n"
        "    patterns: ['*crypto*']\n",
        encoding="utf-8",
    )
    engine = GradingEngine.from_yaml(p)
    assert engine.grade(ops=["data_delete"]).oracle_class == OracleClass.D
    assert engine.grade(["lib/crypto_utils.py"]).oracle_class == OracleClass.C


@pytest.mark.parametrize(
    "path,expected",
    [
        ("db/migrations/0007_add_col.py", OracleClass.D),
        ("infra/main.tf", OracleClass.D),
        ("src/auth/session.py", OracleClass.C),
        ("app/.env.production", OracleClass.C),
        ("src/payment/charge.py", OracleClass.C),
        ("src/api/users.py", OracleClass.B),
        ("src/models.py", OracleClass.B),
        ("docs/readme.md", OracleClass.A),
        ("tests/test_utils.py", OracleClass.A),
    ],
)
def test_builtin_ruleset(path, expected):
    assert GradingEngine.default().grade([path]).oracle_class == expected


@pytest.mark.parametrize("op", ["prod_deploy", "data_delete", "force_push",
                                "schema_migration"])
def test_builtin_ruleset_ops_are_all_hard_gate(op):
    assert GradingEngine.default().grade(ops=[op]).oracle_class == OracleClass.D
