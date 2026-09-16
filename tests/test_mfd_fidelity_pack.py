"""Interface tests for the MFD→XML vertical pack's field-fidelity acceptance harness.

These do NOT convert MFD (no converter source is present on this machine — that is
the recorded blocker). They verify the harness that gates a real conversion: XSD
vocabulary conformance, element coverage, attribute fidelity, and the pass/fail
gate, using the synthetic fixtures shipped with the pack. The real-sample regression
(2.mfd → 我的.xml vs 震总的.xml against 江苏未来数据标准.xsd) is run and reported
separately; it is not committed because it carries real project data.
"""
import types
from pathlib import Path

from factory.control.agent_packs import catalog, pack_zip

FIX = Path(__file__).resolve().parents[1] / (
    "factory/control/builtin_packs/packs/mfd-xml-conversion/fixtures")


def _load_harness():
    # Exec the source directly so no __pycache__/*.pyc is written into the pack
    # fixtures dir (which pack_zip reads as text).
    path = FIX / "fidelity_check.py"
    mod = types.ModuleType("mfd_fidelity_check")
    mod.__file__ = str(path)
    exec(compile(path.read_text(), str(path), "exec"), mod.__dict__)
    return mod


def test_harness_reports_conformance_coverage_and_gaps():
    h = _load_harness()
    report = h.compare(str(FIX / "candidate_sample.xml"),
                       str(FIX / "reference_sample.xml"),
                       str(FIX / "mini_standard.xsd"))
    # A fabricated attribute the XSD does not declare is flagged, not ignored.
    assert ["清单", "乱码"] in report["conformance"]["candidate_undeclared_attributes"]
    assert report["conformance"]["reference_undeclared_attributes"] == []
    # Element type the reference uses that the candidate never emits.
    assert report["element_coverage"]["missing_element_types"] == ["定额"]
    # Attribute the reference fills that the candidate leaves out.
    assert report["attribute_coverage"]["per_element_attribute_gaps"]["清单"] == ["单价", "合价"]
    # Coverage ratios are candidate/reference and below 1 here.
    assert 0 < report["element_coverage"]["ratio"] < 1
    assert 0 < report["attribute_coverage"]["ratio"] < 1


def test_gate_fails_on_undeclared_names_and_low_coverage():
    h = _load_harness()
    report = h.compare(str(FIX / "candidate_sample.xml"),
                       str(FIX / "reference_sample.xml"),
                       str(FIX / "mini_standard.xsd"))
    reasons = h.evaluate(report, min_element_coverage=0.9, min_attribute_coverage=0.9)
    assert reasons  # not empty -> gate fails
    assert any("undeclared attributes" in r for r in reasons)
    assert any("coverage" in r for r in reasons)


def test_gate_passes_when_candidate_matches_reference():
    h = _load_harness()
    # A conforming, fully-covering candidate (the reference compared to itself).
    report = h.compare(str(FIX / "reference_sample.xml"),
                       str(FIX / "reference_sample.xml"),
                       str(FIX / "mini_standard.xsd"))
    assert report["conformance"]["candidate_undeclared_attributes"] == []
    assert report["element_coverage"]["ratio"] == 1.0
    assert report["attribute_coverage"]["ratio"] == 1.0
    assert h.evaluate(report, 0.9, 0.9) == []


def test_mfd_pack_is_in_catalog_and_zips_with_harness_fixture():
    pack = next((p for p in catalog() if p["id"] == "mfd-xml-conversion"), None)
    assert pack is not None
    assert pack["model_settings"] == {} and pack["tool_scope"] == []
    assert len(pack["examples"]) >= 2
    assert all(c["expected"] and c["failure_probe"] for c in pack["examples"])
    # Every example fixture path is bundled into the pack zip.
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(pack_zip("mfd-xml-conversion"))) as z:
        names = z.namelist()
        for case in pack["examples"]:
            if "fixture" in case:
                assert case["fixture"] in names
        assert "fixtures/fidelity_check.py" in names
