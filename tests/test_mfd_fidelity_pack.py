"""Interface + regression tests for the MFD→XML vertical pack's acceptance harness.

These do NOT convert MFD (no converter source is present on this machine — the
recorded blocker). They verify the harness that gates a real conversion:
  * XSD attribute-vocabulary conformance (an element's own attributes only, not a
    child's; attributes matched by full QName; namespaces not blindly dropped),
  * structure/quantity coverage (explicitly not a value-fidelity claim),
  * hierarchical field-value fidelity: records aligned parent-first by reliable
    identity, value changes / duplicate / missing / extra records and values,
    type-driven numeric normalisation, fail-closed on anything unverifiable.

All six "gate wrongly passed" counterexamples Codex reported are pinned below
(three from the first review, three from the second). The real-sample regression
(2.mfd → 我的.xml vs 震总的.xml against the standard XSD) is run and reported
separately; it is not committed because it carries real data.
"""
import types
from pathlib import Path

import pytest

from factory.control.agent_packs import catalog, pack_zip

FIX = Path(__file__).resolve().parents[1] / (
    "factory/control/builtin_packs/packs/mfd-xml-conversion/fixtures")


def _load_harness():
    path = FIX / "fidelity_check.py"
    mod = types.ModuleType("mfd_fidelity_check")
    mod.__file__ = str(path)
    exec(compile(path.read_text(), str(path), "exec"), mod.__dict__)
    return mod


HARNESS = _load_harness()

# A flat no-namespace schema: root holds item; item has id (string), price (decimal),
# 数量 (string). Only item may carry these; root has no own attributes.
_FLAT_XSD = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element ref="item" minOccurs="0" maxOccurs="unbounded"/></xs:sequence></xs:complexType></xs:element>
  <xs:element name="item"><xs:complexType>
    <xs:attribute name="id" type="xs:string"/>
    <xs:attribute name="price" type="xs:decimal"/>
    <xs:attribute name="数量" type="xs:string"/></xs:complexType></xs:element>
</xs:schema>"""
_FLAT_REF = '<root><item id="A" price="100"/><item id="B" price="200"/></root>'

# A nested schema: root > group(id) > item(id, price). For the parent-transposition case.
_NEST_XSD = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element ref="group" minOccurs="0" maxOccurs="unbounded"/></xs:sequence></xs:complexType></xs:element>
  <xs:element name="group"><xs:complexType><xs:sequence>
    <xs:element ref="item" minOccurs="0" maxOccurs="unbounded"/></xs:sequence>
    <xs:attribute name="id" type="xs:string"/></xs:complexType></xs:element>
  <xs:element name="item"><xs:complexType>
    <xs:attribute name="id" type="xs:string"/>
    <xs:attribute name="price" type="xs:decimal"/></xs:complexType></xs:element>
</xs:schema>"""
_NEST_REF = ('<root><group id="G1"><item id="A" price="100"/></group>'
             '<group id="G2"><item id="B" price="200"/></group></root>')


def _write(tmp_path, name, text):
    p = Path(tmp_path) / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _report(tmp_path, xsd, ref, candidate_xml, xsd_name="s.xsd"):
    xsd_p = _write(tmp_path, xsd_name, xsd)
    ref_p = _write(tmp_path, "ref.xml", ref)
    cand_p = _write(tmp_path, "cand.xml", candidate_xml)
    return HARNESS.compare(cand_p, ref_p, xsd_p)


# --------------------------------------------------- first review's counterexamples

def test_value_change_is_caught(tmp_path):
    r = _report(tmp_path, _FLAT_XSD, _FLAT_REF,
                '<root><item id="A" price="999"/><item id="B" price="999"/></root>')
    assert len(r["field_value_fidelity"]["value_changes"]) == 2
    assert HARNESS.evaluate(r, 0.0, 0.0)


def test_record_substitution_is_caught(tmp_path):
    r = _report(tmp_path, _FLAT_XSD, _FLAT_REF,
                '<root><item id="A" price="100"/><item id="A" price="100"/></root>')
    fid = r["field_value_fidelity"]
    assert any(d["key"] == "id=A" for d in fid["duplicate_records"])
    assert any(m.get("key") == "id=B" for m in fid["missing_records"])
    assert HARNESS.evaluate(r, 0.0, 0.0)


def test_undeclared_parent_attribute_is_caught(tmp_path):
    r = _report(tmp_path, _FLAT_XSD, _FLAT_REF,
                '<root price="5"><item id="A" price="100"/><item id="B" price="200"/></root>')
    assert ["root", "price"] in r["xsd_conformance"]["candidate_undeclared_attributes"]
    assert HARNESS.evaluate(r, 0.0, 0.0)


# -------------------------------------------------- second review's counterexamples

def test_parent_transposition_is_caught(tmp_path):
    # Items swapped between parent groups; ids/prices unchanged. Hierarchical
    # alignment must not stitch a global item table across parents.
    swapped = ('<root><group id="G1"><item id="B" price="200"/></group>'
               '<group id="G2"><item id="A" price="100"/></group></root>')
    r = _report(tmp_path, _NEST_XSD, _NEST_REF, swapped)
    fid = r["field_value_fidelity"]
    assert fid["missing_records"] and fid["extra_records"]
    assert HARNESS.evaluate(r, 0.0, 0.0)


def test_namespaced_attribute_is_not_a_substitute(tmp_path):
    r = _report(tmp_path, _FLAT_XSD, '<root><item id="A" price="100"/></root>',
                '<root xmlns:f="urn:fake"><item id="A" f:price="100"/></root>')
    conf = r["xsd_conformance"]
    assert ["item", "{urn:fake}price"] in conf["candidate_namespaced_attributes_unverified"]
    # the real (no-namespace) price is now missing on the candidate
    assert any(m["attr"] == "price" for m in r["field_value_fidelity"]["missing_values"])
    assert HARNESS.evaluate(r, 0.0, 0.0)


def test_extra_record_fails_by_default_but_can_be_allowed(tmp_path):
    r = _report(tmp_path, _FLAT_XSD, '<root><item id="A" price="100"/></root>',
                '<root><item id="A" price="100"/><item id="B" price="999"/></root>')
    assert any(e.get("key") == "id=B" for e in r["field_value_fidelity"]["extra_records"])
    assert HARNESS.evaluate(r, 0.0, 0.0)                    # default: extra records fail
    assert HARNESS.evaluate(r, 0.0, 0.0, allow_extra=True) == []  # explicit policy allows


# --------------------------------------------------------------- value semantics

def test_numeric_fields_normalise_but_string_ids_do_not(tmp_path):
    ref = '<root><item id="A" price="100" 数量="001"/></root>'
    # price is xs:decimal: 100 == 100.00 (equal); 数量 is xs:string: "001" != "1".
    equal = _report(tmp_path, _FLAT_XSD, ref, '<root><item id="A" price="100.00" 数量="001"/></root>')
    assert equal["field_value_fidelity"]["value_changes"] == []
    changed = _report(tmp_path, _FLAT_XSD, ref, '<root><item id="A" price="100" 数量="1"/></root>')
    assert any(v["attr"] == "数量" for v in changed["field_value_fidelity"]["value_changes"])


def test_non_finite_number_never_equal(tmp_path):
    ref = '<root><item id="A" price="100"/></root>'
    r = _report(tmp_path, _FLAT_XSD, ref, '<root><item id="A" price="NaN"/></root>')
    assert any(v["attr"] == "price" for v in r["field_value_fidelity"]["value_changes"])
    assert HARNESS.evaluate(r, 0.0, 0.0)


def test_text_content_is_unverified_not_passed(tmp_path):
    xsd = ('<?xml version="1.0"?><xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
           '<xs:element name="root"><xs:complexType><xs:sequence>'
           '<xs:element ref="item" minOccurs="0" maxOccurs="unbounded"/></xs:sequence></xs:complexType></xs:element>'
           '<xs:element name="item"><xs:complexType><xs:simpleContent><xs:extension base="xs:string">'
           '<xs:attribute name="id" type="xs:string"/></xs:extension></xs:simpleContent></xs:complexType></xs:element>'
           '</xs:schema>')
    doc = '<root><item id="A">hello</item></root>'
    r = _report(tmp_path, xsd, doc, doc)
    assert r["xsd_conformance"]["candidate_text_content_unverified"] == ["item"]
    assert HARNESS.evaluate(r, 0.0, 0.0)  # identical docs still fail: text is unverifiable here


def test_threshold_validation_rejects_nan_and_out_of_range(tmp_path):
    r = _report(tmp_path, _FLAT_XSD, _FLAT_REF, _FLAT_REF)
    for bad in (float("nan"), float("inf"), 1.5, -0.1, "0.9", True):
        with pytest.raises(ValueError):
            HARNESS.evaluate(r, bad, 0.9)


def test_structure_coverage_is_separate_from_fidelity(tmp_path):
    r = _report(tmp_path, _FLAT_XSD, _FLAT_REF,
                '<root><item id="A" price="100"/><item id="A" price="100"/></root>')
    assert r["structure_quantity_coverage"]["element_ratio"] == 1.0  # full quantity
    assert "caveat" in r["structure_quantity_coverage"]
    assert HARNESS.evaluate(r, 0.9, 0.9)  # yet fidelity fails


def test_supported_document_passes(tmp_path):
    # A conforming, fully-covering candidate (reference vs itself) passes the gate.
    r = _report(tmp_path, _NEST_XSD, _NEST_REF, _NEST_REF)
    assert r["field_value_fidelity"]["value_changes"] == []
    assert HARNESS.evaluate(r, 0.9, 0.9) == []


# --------------------------------------------------------- shipped pack fixtures

def test_synthetic_pack_fixtures_show_conformance_and_gaps():
    r = HARNESS.compare(str(FIX / "candidate_sample.xml"),
                        str(FIX / "reference_sample.xml"),
                        str(FIX / "mini_standard.xsd"))
    conf = r["xsd_conformance"]
    assert ["清单", "乱码"] in conf["candidate_undeclared_attributes"]
    assert r["structure_quantity_coverage"]["missing_element_types"] == ["定额"]
    missing_attrs = {m["attr"] for m in r["field_value_fidelity"]["missing_values"]}
    assert {"单价", "合价"} <= missing_attrs
    assert HARNESS.evaluate(r, 0.9, 0.9)


def test_faithful_candidate_passes_gate():
    r = HARNESS.compare(str(FIX / "reference_sample.xml"),
                        str(FIX / "reference_sample.xml"),
                        str(FIX / "mini_standard.xsd"))
    assert r["field_value_fidelity"]["value_changes"] == []
    assert HARNESS.evaluate(r, 0.9, 0.9) == []


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


def test_nonempty_tail_text_is_unverified(tmp_path):
    candidate = _FLAT_REF.replace('</root>', 'unexpected price 999</root>')
    report = _report(tmp_path, _FLAT_XSD, _FLAT_REF, candidate)
    assert report['xsd_conformance']['candidate_text_content_unverified']
    assert HARNESS.evaluate(report)


def test_formatting_tail_whitespace_remains_supported(tmp_path):
    candidate = _FLAT_REF.replace('</root>', '\n  </root>')
    report = _report(tmp_path, _FLAT_XSD, _FLAT_REF, candidate)
    assert HARNESS.evaluate(report) == []
