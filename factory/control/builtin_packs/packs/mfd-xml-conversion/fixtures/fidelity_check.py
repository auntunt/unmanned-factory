#!/usr/bin/env python3
"""Field-fidelity / coverage acceptance harness for MFD -> XML conversion.

This is the *acceptance* half of the vertical pack: it does not convert anything.
Given a candidate XML (the converter's output), a reference XML (a trusted
conversion of the same MFD), and the Jiangsu-standard XSD, it reports, using only
the Python standard library:

  1. XSD conformance — every element/attribute the candidate emits must be a name
     the XSD declares. Undeclared names are fabricated structure and fail the check.
  2. Element coverage — which XSD-declared element types the reference populates
     that the candidate never emits.
  3. Attribute fidelity — for shared element types, which attributes the reference
     fills that the candidate leaves out.
  4. Totals — element and filled-attribute coverage ratios candidate/reference.

It is deliberately conservative: with only stdlib it checks the element/attribute
*vocabulary* against the XSD, not the full content model. Full XSD validation
(sequence/occurrence) needs lxml and is noted as a gap, never silently claimed.

CLI:
    python fidelity_check.py CANDIDATE.xml REFERENCE.xml STANDARD.xsd \
        [--min-element-coverage 0.9] [--strict]

Exit code is 0 when the report is produced. With --strict it is non-zero when the
candidate emits undeclared names, or when coverage is below the given thresholds —
so the same harness can gate a real conversion in CI once the converter exists.
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

_XS = "{http://www.w3.org/2001/XMLSchema}"


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def parse_xsd(path: str) -> dict[str, set[str]]:
    """Map each xs:element name to the set of attribute names declared under it."""
    root = ET.parse(path).getroot()
    elems: dict[str, set[str]] = {}
    for el in root.iter(_XS + "element"):
        name = el.get("name")
        if not name:
            continue
        attrs = {a.get("name") for a in el.iter(_XS + "attribute") if a.get("name")}
        elems.setdefault(name, set()).update(attrs)
    return elems


def xml_stats(path: str):
    """Return (element counts, {element: {attr: filled_count}}) for one document.

    An attribute counts as *filled* only when its value is non-empty after strip;
    empty scaffolding attributes are not credited as populated data.
    """
    root = ET.parse(path).getroot()
    tags: Counter = Counter()
    filled: dict[str, Counter] = defaultdict(Counter)
    for e in root.iter():
        name = _local(e.tag)
        tags[name] += 1
        for key, value in e.attrib.items():
            if value is not None and str(value).strip() != "":
                filled[name][key] += 1
    return tags, filled


def _undeclared(tags: Counter, filled: dict[str, Counter], xsd: dict[str, set[str]]):
    elements = sorted(t for t in tags if t not in xsd)
    attributes = sorted(
        {(t, a) for t, attrs in filled.items() if t in xsd for a in attrs if a not in xsd[t]}
    )
    return elements, [list(pair) for pair in attributes]


def compare(candidate: str, reference: str, xsd_path: str) -> dict:
    """Produce the full fidelity report as a plain dict (JSON-serializable)."""
    xsd = parse_xsd(xsd_path)
    ct, cf = xml_stats(candidate)
    rt, rf = xml_stats(reference)

    cand_undeclared_elems, cand_undeclared_attrs = _undeclared(ct, cf, xsd)
    ref_undeclared_elems, ref_undeclared_attrs = _undeclared(rt, rf, xsd)

    # Element types the reference populates that the candidate never emits.
    missing_elements = sorted(t for t in rt if t in xsd and rt[t] > 0 and ct.get(t, 0) == 0)
    # Element types the candidate emits that the reference does not (often empty scaffolding).
    extra_elements = sorted(t for t in ct if t in xsd and ct[t] > 0 and rt.get(t, 0) == 0)

    # Attribute fidelity for element types both sides emit.
    attribute_gaps: dict[str, list[str]] = {}
    for t in sorted(set(cf) & set(rf)):
        only_ref = sorted(set(rf[t]) - set(cf[t]))
        if only_ref:
            attribute_gaps[t] = only_ref

    cand_elems = sum(ct.values())
    ref_elems = sum(rt.values())
    cand_filled = sum(sum(c.values()) for c in cf.values())
    ref_filled = sum(sum(c.values()) for c in rf.values())

    def ratio(a: int, b: int) -> float:
        return round(a / b, 4) if b else (1.0 if a == 0 else 0.0)

    return {
        "xsd_declared_elements": len(xsd),
        "conformance": {
            "candidate_undeclared_elements": cand_undeclared_elems,
            "candidate_undeclared_attributes": cand_undeclared_attrs,
            "reference_undeclared_elements": ref_undeclared_elems,
            "reference_undeclared_attributes": ref_undeclared_attrs,
        },
        "element_coverage": {
            "candidate_total": cand_elems,
            "reference_total": ref_elems,
            "ratio": ratio(cand_elems, ref_elems),
            "missing_element_types": missing_elements,
            "extra_element_types": extra_elements,
        },
        "attribute_coverage": {
            "candidate_filled_total": cand_filled,
            "reference_filled_total": ref_filled,
            "ratio": ratio(cand_filled, ref_filled),
            "per_element_attribute_gaps": attribute_gaps,
        },
        "note": "stdlib vocabulary check only; full XSD content-model validation requires lxml.",
    }


def evaluate(report: dict, min_element_coverage: float, min_attribute_coverage: float) -> list[str]:
    """Return a list of failure reasons; empty means the candidate passes the gate."""
    reasons = []
    c = report["conformance"]
    if c["candidate_undeclared_elements"]:
        reasons.append(f"candidate emits undeclared elements: {c['candidate_undeclared_elements']}")
    if c["candidate_undeclared_attributes"]:
        reasons.append(f"candidate emits undeclared attributes: {c['candidate_undeclared_attributes']}")
    if report["element_coverage"]["ratio"] < min_element_coverage:
        reasons.append(
            f"element coverage {report['element_coverage']['ratio']:.1%} < {min_element_coverage:.0%}")
    if report["attribute_coverage"]["ratio"] < min_attribute_coverage:
        reasons.append(
            f"attribute coverage {report['attribute_coverage']['ratio']:.1%} < {min_attribute_coverage:.0%}")
    return reasons


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="MFD->XML field-fidelity acceptance harness")
    p.add_argument("candidate")
    p.add_argument("reference")
    p.add_argument("xsd")
    p.add_argument("--min-element-coverage", type=float, default=0.9)
    p.add_argument("--min-attribute-coverage", type=float, default=0.9)
    p.add_argument("--strict", action="store_true",
                   help="exit non-zero when undeclared names appear or coverage is below thresholds")
    args = p.parse_args(argv)
    report = compare(args.candidate, args.reference, args.xsd)
    reasons = evaluate(report, args.min_element_coverage, args.min_attribute_coverage)
    report["gate"] = {"passed": not reasons, "reasons": reasons}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict and reasons:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
