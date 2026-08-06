from factory.audit.models import OracleClass
from factory.routing import Router


def test_model_escalates_with_attempt_no():
    r = Router({"A": ["haiku", "sonnet", "opus"]}, default=["sonnet"])
    assert r.model_for(OracleClass.A, 1) == "haiku"
    assert r.model_for(OracleClass.A, 2) == "sonnet"
    assert r.model_for(OracleClass.A, 3) == "opus"


def test_model_clamps_beyond_ladder():
    r = Router({"A": ["haiku", "opus"]}, default=["sonnet"])
    assert r.model_for(OracleClass.A, 9) == "opus"


def test_unknown_class_uses_default():
    r = Router({}, default=["sonnet"])
    assert r.model_for(OracleClass.B, 1) == "sonnet"


def test_from_yaml(tmp_path):
    p = tmp_path / "routing.yaml"
    p.write_text(
        "default: [sonnet]\nby_class:\n  A: [haiku, sonnet]\n  B: [sonnet, opus]\n",
        encoding="utf-8",
    )
    r = Router.from_yaml(p)
    assert r.model_for(OracleClass.A, 1) == "haiku"
    assert r.model_for(OracleClass.B, 2) == "opus"


def test_builtin_table_starts_cheap_for_class_a():
    r = Router.default()
    assert r.model_for(OracleClass.A, 1) == "haiku"
    assert r.model_for(OracleClass.A, 3) == "opus"


def test_class_c_and_d_pin_to_strongest_model():
    """C/D 永不无人，但万一人工放行后仍走本管道，要有强模型兜底。"""
    r = Router.default()
    assert r.model_for(OracleClass.C, 1) == "opus"
    assert r.model_for(OracleClass.D, 5) == "opus"
