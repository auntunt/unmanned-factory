"""Interface + regression tests for the MFD→XML vertical pack's acceptance harness.

These do NOT convert MFD (no converter source is present on this machine — the
recorded blocker). They verify the harness that gates a real conversion:
  * XSD attribute-vocabulary conformance (an element's own attributes only, not a
    child's; namespaces not blindly dropped),
  * structure/quantity coverage (explicitly not a value-fidelity claim),
  * field-value fidelity: value changes, duplicate and missing records aligned by
    reliable identity, with numeric normalisation and threshold validation.

The three "gate wrongly passed" counterexamples Codex reported are pinned below.
The real-sample regression (2.mfd → 我的.xml vs 震总的.xml against the standard XSD)
is run and reported separately; it is not committed because it carries real data.
"""
import types
from pathlib import Path

import pytest

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


HARNESS = _load_harness()


def _write(dirpath, name, text):
    p = Path(dirpath) / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# A minimal no-namespace schema: root holds item; only item may carry id/price.
_XSD = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence><xs:element ref="item" minOccurs="0" maxOccurs="unbounded"/></xs:sequence>
    </xs:complexType>
  </xs:element>
  <xs:element name="item">
    <xs:complexType>
      <xs:attribute name="id" type="xs:string"/>
      <xs:attribute name="price" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""
_REF = '<root><item id="A" price="100"/><item id="B" price="200"/></root>'


def _report(tmp_path, candidate_xml):
    xsd = _write(tmp_path, "s.xsd", _XSD)
    ref = _write(tmp_path, "ref.xml", _REF)
    cand = _write(tmp_path, "cand.xml", candidate_xml)
    return HARNESS.compare(cand, ref, xsd)


def test_counterexample_value_change_is_caught(tmp_path):
    # Same ids, prices altered — must not pass as a count-only match.
    report = _report(tmp_path, '<root><item id="A" price="999"/><item id="B" price="999"/></root>')
    assert len(report["field_value_fidelity"]["value_changes"]) == 2
    assert HARNESS.evaluate(report, 0.0, 0.0)  # gate fails even at zero coverage thresholds


def test_counterexample_record_substitution_is_caught(tmp_path):
    # Two A/100 instead of A + B — duplicate A and missing B; extra must not offset.
    report = _report(tmp_path, '<root><item id="A" price="100"/><item id="A" price="100"/></root>')
    fid = report["field_value_fidelity"]
    assert any(d["key"] == "id=A" for d in fid["duplicate_records"])
    assert any(m.get("key") == "id=B" for m in fid["missing_records"])
    assert HARNESS.evaluate(report, 0.0, 0.0)


def test_counterexample_undeclared_parent_attribute_is_caught(tmp_path):
    # price on <root> is a child attribute; the parent must not inherit it.
    report = _report(tmp_path, '<root price="5"><item id="A" price="100"/><item id="B" price="200"/></root>')
    assert ["root", "price"] in report["xsd_conformance"]["candidate_undeclared_attributes"]
    assert HARNESS.evaluate(report, 0.0, 0.0)


def test_numeric_normalisation_allows_equal_values(tmp_path):
    report = _report(tmp_path, '<root><item id="A" price="100.00"/><item id="B" price="200.0"/></root>')
    assert report["field_value_fidelity"]["value_changes"] == []
    assert HARNESS.evaluate(report, 0.9, 0.9) == []  # a faithful candidate passes


def test_threshold_validation_rejects_nan_and_out_of_range(tmp_path):
    report = _report(tmp_path, _REF)
    for bad in (float("nan"), 1.5, -0.1, "0.9", True):
        with pytest.raises(ValueError):
            HARNESS.evaluate(report, bad, 0.9)


def test_structure_coverage_is_separate_from_fidelity(tmp_path):
    # Record substitution keeps element counts identical (100% quantity) yet fails fidelity.
    report = _report(tmp_path, '<root><item id="A" price="100"/><item id="A" price="100"/></root>')
    assert report["structure_quantity_coverage"]["element_ratio"] == 1.0
    assert "caveat" in report["structure_quantity_coverage"]
    assert HARNESS.evaluate(report, 0.9, 0.9)  # fidelity failure despite full quantity coverage


def test_synthetic_pack_fixtures_show_conformance_and_gaps():
    report = HARNESS.compare(str(FIX / "candidate_sample.xml"),
                             str(FIX / "reference_sample.xml"),
                             str(FIX / "mini_standard.xsd"))
    conf = report["xsd_conformance"]
    assert ["清单", "乱码"] in conf["candidate_undeclared_attributes"]
    assert report["structure_quantity_coverage"]["missing_element_types"] == ["定额"]
    fid = report["field_value_fidelity"]
    # 清单 aligned by 编号: 单价/合价 present in reference, absent in candidate.
    missing_attrs = {m["attr"] for m in fid["missing_values"]}
    assert {"单价", "合价"} <= missing_attrs
    assert HARNESS.evaluate(report, 0.9, 0.9)


def test_faithful_candidate_passes_gate():
    report = HARNESS.compare(str(FIX / "reference_sample.xml"),
                             str(FIX / "reference_sample.xml"),
                             str(FIX / "mini_standard.xsd"))
    assert report["structure_quantity_coverage"]["element_ratio"] == 1.0
    assert report["field_value_fidelity"]["value_changes"] == []
    assert HARNESS.evaluate(report, 0.9, 0.9) == []


def test_mfd_pack_is_in_catalog_and_zips_with_harness_fixture():
    pack = next((p for p in catalog() if p["id"] == "mfd-xml-conversion"), None)
    assert pack is not None
    assert pack["model_settings"] == {} and pack["tool_scope"] == []
    assert len(pack["examples"]) >= 2
    assert all(c["expected"] and c["failure_probe"] for c in pack["examples"])
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(pack_zip("mfd-xml-conversion"))) as z:
        names = z.namelist()
        for case in pack["examples"]:
            if "fixture" in case:
                assert case["fixture"] in names
        assert "fixtures/fidelity_check.py" in names
