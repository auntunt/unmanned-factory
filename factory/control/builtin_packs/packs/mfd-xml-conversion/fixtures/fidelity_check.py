#!/usr/bin/env python3
"""Acceptance harness for MFD -> XML conversion. It does NOT convert anything.

Given a candidate XML (a converter's output), a reference XML (a trusted conversion
of the SAME MFD), and the Jiangsu-standard XSD, it reports three separate things,
using only the Python standard library, and FAILS CLOSED on anything it cannot
verify rather than passing it:

  1. xsd_conformance — every element/attribute the candidate emits must be a name
     the XSD declares for that element itself (a parent never inherits a child's
     attributes). Attributes are matched by full QName; a namespaced attribute is
     not schema-supported here and is marked unverified. Elements in a namespace
     other than the schema's target are unverified. Element text content, and
     schema features this stdlib check cannot resolve (include/import/group/any/
     mixed/substitutionGroup, attribute groups/refs, named complex types), are
     reported as unverified — never silently accepted.

  2. structure_quantity_coverage — element-instance and filled-attribute counts
     relative to the reference. A *quantity* signal only: high coverage does NOT
     mean the values or the record placement are correct.

  3. field_value_fidelity — records are aligned HIERARCHICALLY: parents are aligned
     by a reliable identity (an ``*ID`` / 编号 / 名称 / 序号 attribute present and
     unique among siblings), then children are compared only within a matched
     parent. A record moved to a different parent is therefore caught. When a group
     has no reliable identity (and is not an unambiguous singleton) it is marked
     unverified and its children are NOT compared — no cross-parent global table.
     Values are compared per XSD-declared type: only numeric-typed attributes are
     normalised with Decimal (so "100" == "100.00"), while identity/string fields
     such as 编号="001" compare exactly. Non-finite numbers (NaN/Inf) never compare
     equal. Extra records and extra filled values are reported and, by default,
     fail the gate (a business policy may allow them explicitly via --allow-extra).

Full XSD content-model validation (element order/occurrence) needs lxml, which is
optional and not a declared dependency of this repo; this harness never claims it.

CLI:
    python fidelity_check.py CANDIDATE.xml REFERENCE.xml STANDARD.xsd \
        [--min-element-coverage 0.9] [--min-attribute-coverage 0.9] \
        [--allow-extra] [--strict]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

_XS = "{http://www.w3.org/2001/XMLSchema}"
_MAX_DETAIL = 200
_NUMERIC_BUILTINS = {
    "decimal", "integer", "int", "long", "short", "byte", "double", "float",
    "nonNegativeInteger", "positiveInteger", "negativeInteger", "nonPositiveInteger",
    "unsignedInt", "unsignedLong", "unsignedShort", "unsignedByte",
}
_ID_FALLBACKS = ("编号", "名称", "id", "序号")


def _qn(tag: str):
    """Return (namespace_uri_or_None, local_name), preserving the namespace."""
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return ns, local
    return None, tag


def _local(tag: str) -> str:
    return _qn(tag)[1]


def _no_ns_attrs(elem) -> dict:
    """Attributes in no namespace, keyed by local name (schema attrs are unqualified)."""
    return {_qn(k)[1]: v for k, v in elem.attrib.items() if _qn(k)[0] is None}


# --------------------------------------------------------------------------- XSD

def parse_xsd(path: str):
    """Return (target_namespace, declared, unsupported).

    declared: {element_local: {attr_local: {"numeric": bool}}} — an element's OWN
    attributes only (walk stops at nested xs:element). unsupported: schema features
    this stdlib check cannot verify, so the gate fails closed on them.
    """
    root = ET.parse(path).getroot()
    target_ns = root.get("targetNamespace")
    unsupported: set[str] = set()

    simple_base = {}
    for st in root.iter(_XS + "simpleType"):
        name = st.get("name")
        if not name:
            continue
        restr = st.find(_XS + "restriction")
        simple_base[name] = restr.get("base") if restr is not None else None
    named_complex = {ct.get("name") for ct in root.iter(_XS + "complexType") if ct.get("name")}

    for feat in ("include", "import", "redefine", "group", "any", "anyAttribute"):
        if root.find(".//" + _XS + feat) is not None:
            unsupported.add("xs:" + feat)
    for ct in root.iter(_XS + "complexType"):
        if ct.get("mixed") == "true":
            unsupported.add("mixed content")
    for el in root.iter(_XS + "element"):
        if el.get("substitutionGroup"):
            unsupported.add("substitutionGroup")
        etype = el.get("type")
        if etype and not etype.startswith("xs:") and etype in named_complex:
            unsupported.add(f"element typed by named complexType {etype!r}")

    def numeric_of_type(t, seen=()):
        if t is None:
            return False
        if t.startswith("xs:"):
            return t.split(":", 1)[1] in _NUMERIC_BUILTINS
        if t in named_complex:
            unsupported.add(f"attribute typed by named complexType {t!r}")
            return False
        if t not in simple_base:
            unsupported.add(f"unresolved named type {t!r}")
            return False
        if t in seen:
            return False
        return numeric_of_type(simple_base[t], seen + (t,))

    def own_attributes(element) -> dict:
        res: dict[str, dict] = {}

        def walk(node):
            for child in node:
                lt = _local(child.tag)
                if lt == "element":
                    continue  # boundary: nested element's attributes belong to it
                if lt == "attribute":
                    name = child.get("name")
                    if name:
                        t = child.get("type")
                        if t is None:
                            st = child.find(_XS + "simpleType")
                            base = None
                            if st is not None:
                                restr = st.find(_XS + "restriction")
                                base = restr.get("base") if restr is not None else None
                            res[name] = {"numeric": numeric_of_type(base)}
                        else:
                            res[name] = {"numeric": numeric_of_type(t)}
                    elif child.get("ref"):
                        unsupported.add("attribute ref")
                    continue
                if lt == "attributeGroup":
                    unsupported.add("attributeGroup")
                    continue
                walk(child)

        ctype = element.find(_XS + "complexType")
        if ctype is not None:
            walk(ctype)
        return res

    declared: dict[str, dict] = {}
    for el in root.iter(_XS + "element"):
        name = el.get("name")
        if not name:
            continue
        declared.setdefault(name, {}).update(own_attributes(el))
    return target_ns, declared, sorted(unsupported)


# ------------------------------------------------------------------ conformance

def _conformance(root_elem, declared, target_ns):
    undeclared_elems, undeclared_attrs = set(), set()
    namespaced_attrs, unverified_ns, text_elems = set(), set(), set()

    def walk(e):
        ns, local = _qn(e.tag)
        if (e.text and e.text.strip()) or (e.tail and e.tail.strip()):
            text_elems.add(local)
        if ns != target_ns:
            unverified_ns.add(f"{{{ns}}}{local}" if ns else local)
        else:
            if local not in declared:
                undeclared_elems.add(local)
            else:
                for key in e.attrib:
                    ans, al = _qn(key)
                    if ans is not None:
                        namespaced_attrs.add((local, f"{{{ans}}}{al}"))
                    elif al not in declared[local]:
                        undeclared_attrs.add((local, al))
        for child in e:
            walk(child)

    walk(root_elem)
    return {
        "undeclared_elements": sorted(undeclared_elems),
        "undeclared_attributes": [list(p) for p in sorted(undeclared_attrs)],
        "namespaced_attributes": [list(p) for p in sorted(namespaced_attrs)],
        "unverified_namespaces": sorted(unverified_ns),
        "text_content": sorted(text_elems),
    }


# ----------------------------------------------------------------- value helpers

def _num(value):
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None  # NaN/Inf -> None -> never equal


def _values_equal(a, b, numeric: bool) -> bool:
    if numeric:
        na, nb = _num(a), _num(b)
        if na is None or nb is None:
            return False  # a non-finite or unparseable value in a numeric field is a mismatch
        return na == nb
    return str(a).strip() == str(b).strip()  # identity/text fields: exact, so "001" != "1"


def _choose_key(records):
    """An attribute present and unique across the sibling group, preferring an *ID
    name, else 编号/名称/id/序号. Only no-namespace attributes are eligible."""
    if not records:
        return None
    attr_sets = [set(_no_ns_attrs(r)) for r in records]
    attrs = set().union(*attr_sets) if attr_sets else set()

    def unique(attr):
        vals = [_no_ns_attrs(r).get(attr, "").strip() for r in records]
        return all(vals) and len(set(vals)) == len(vals)

    for attr in sorted(a for a in attrs if a.endswith("ID")):
        if unique(attr):
            return attr
    for attr in _ID_FALLBACKS:
        if attr in attrs and unique(attr):
            return attr
    return None


def _compare_attrs(cand_elem, ref_elem, loc, key_desc, declared, result):
    elem_local = _local(ref_elem.tag)
    numeric_map = declared.get(elem_local, {})
    ref_no_ns = _no_ns_attrs(ref_elem)
    cand_no_ns = _no_ns_attrs(cand_elem)

    def tag(entry):
        if key_desc:
            entry["key"] = key_desc
        return entry

    for attr, rv in ref_no_ns.items():
        if not str(rv).strip():
            continue
        cv = cand_no_ns.get(attr)
        numeric = numeric_map.get(attr, {}).get("numeric", False)
        if cv is None or not str(cv).strip():
            result["missing_values"].append(tag({"location": loc, "attr": attr}))
        elif not _values_equal(rv, cv, numeric):
            result["value_changes"].append(tag({"location": loc, "attr": attr, "reference": rv, "candidate": cv}))
    for attr, cv in cand_no_ns.items():
        if not str(cv).strip():
            continue
        if not str(ref_no_ns.get(attr, "")).strip():
            result["extra_values"].append(tag({"location": loc, "attr": attr, "candidate": cv}))


def _diff_children(cand_elem, ref_elem, loc, declared, result):
    def by_tag(elem):
        groups = defaultdict(list)
        for child in elem:
            groups[child.tag].append(child)
        return groups

    rg, cg = by_tag(ref_elem), by_tag(cand_elem)
    for tag, rrecs in rg.items():
        crecs = cg.get(tag, [])
        _align(rrecs, crecs, loc + "/" + _local(tag), declared, result)
    for tag, crecs in cg.items():
        if tag not in rg:
            result["extra_records"].append(
                {"location": loc + "/" + _local(tag), "extra_count": len(crecs), "not_in_reference": True})


def _align(rrecs, crecs, loc, declared, result):
    key = _choose_key(rrecs)
    cand_has_key = bool(key) and all(_no_ns_attrs(r).get(key, "").strip() for r in crecs)

    if not key or not cand_has_key:
        if len(rrecs) == 1 and len(crecs) == 1:  # unambiguous singleton
            _compare_attrs(crecs[0], rrecs[0], loc, None, declared, result)
            _diff_children(crecs[0], rrecs[0], loc, declared, result)
            return
        if len(crecs) < len(rrecs):
            result["missing_records"].append({"location": loc, "missing_count": len(rrecs) - len(crecs), "unverified": True})
        elif len(crecs) > len(rrecs):
            result["extra_records"].append({"location": loc, "extra_count": len(crecs) - len(rrecs), "unverified": True})
        result["unverified_groups"].append({
            "location": loc, "reference_count": len(rrecs), "candidate_count": len(crecs),
            "reason": "no reliable identity attribute" if not key else "candidate lacks the identity attribute"})
        return  # cannot align -> children unverified, do NOT recurse

    rmap, cmap = {}, {}
    rc = Counter(_no_ns_attrs(r).get(key, "").strip() for r in rrecs)
    cc = Counter(_no_ns_attrs(r).get(key, "").strip() for r in crecs)
    for k, c in cc.items():
        if c > 1:
            result["duplicate_records"].append({"location": loc, "key": f"{key}={k}", "count": c, "side": "candidate"})
    for k, c in rc.items():
        if c > 1:
            result["duplicate_records"].append({"location": loc, "key": f"{key}={k}", "count": c, "side": "reference"})
    for r in rrecs:
        rmap.setdefault(_no_ns_attrs(r).get(key, "").strip(), r)
    for r in crecs:
        cmap.setdefault(_no_ns_attrs(r).get(key, "").strip(), r)
    for k in rmap:
        if k not in cmap:
            result["missing_records"].append({"location": loc, "key": f"{key}={k}"})
    for k in cmap:
        if k not in rmap:
            result["extra_records"].append({"location": loc, "key": f"{key}={k}"})
    for k in rmap:
        if k in cmap:
            _compare_attrs(cmap[k], rmap[k], loc, f"{key}={k}", declared, result)
            _diff_children(cmap[k], rmap[k], loc, declared, result)


def _diff(cand_root, ref_root, declared, result):
    if cand_root.tag != ref_root.tag:
        result["missing_records"].append(
            {"location": _local(ref_root.tag), "reason": f"root mismatch: candidate {_local(cand_root.tag)!r}"})
        return
    loc = _local(ref_root.tag)
    _compare_attrs(cand_root, ref_root, loc, None, declared, result)
    _diff_children(cand_root, ref_root, loc, declared, result)


# --------------------------------------------------------------------- assembly

def _quantity_coverage(cand_root, ref_root, declared):
    def counts(root):
        tags = Counter()
        filled = 0
        for e in root.iter():
            tags[_local(e.tag)] += 1
            filled += sum(1 for v in e.attrib.values() if v is not None and str(v).strip() != "")
        return tags, sum(tags.values()), filled

    ct, cand_elems, cand_filled = counts(cand_root)
    rt, ref_elems, ref_filled = counts(ref_root)

    def ratio(a, b):
        return round(a / b, 4) if b else (1.0 if a == 0 else 0.0)

    return {
        "candidate_element_total": cand_elems,
        "reference_element_total": ref_elems,
        "element_ratio": ratio(cand_elems, ref_elems),
        "candidate_filled_attribute_total": cand_filled,
        "reference_filled_attribute_total": ref_filled,
        "attribute_ratio": ratio(cand_filled, ref_filled),
        "missing_element_types": sorted(t for t in rt if t in declared and rt[t] > 0 and ct.get(t, 0) == 0),
        "extra_element_types": sorted(t for t in ct if t in declared and ct[t] > 0 and rt.get(t, 0) == 0),
        "caveat": "quantity/structure signal only; NOT a field-value fidelity result.",
    }


def compare(candidate: str, reference: str, xsd_path: str) -> dict:
    target_ns, declared, unsupported = parse_xsd(xsd_path)
    cand_root = ET.parse(candidate).getroot()
    ref_root = ET.parse(reference).getroot()

    cc = _conformance(cand_root, declared, target_ns)
    rc = _conformance(ref_root, declared, target_ns)

    fid = {"value_changes": [], "missing_values": [], "extra_values": [],
           "missing_records": [], "extra_records": [], "duplicate_records": [], "unverified_groups": []}
    _diff(cand_root, ref_root, declared, fid)
    for name, items in fid.items():
        if len(items) > _MAX_DETAIL:
            fid[name] = items[:_MAX_DETAIL] + [{"_truncated": len(items) - _MAX_DETAIL}]

    return {
        "xsd_declared_elements": len(declared),
        "xsd_target_namespace": target_ns,
        "xsd_conformance": {
            "candidate_undeclared_elements": cc["undeclared_elements"],
            "candidate_undeclared_attributes": cc["undeclared_attributes"],
            "candidate_namespaced_attributes_unverified": cc["namespaced_attributes"],
            "candidate_unverified_namespaces": cc["unverified_namespaces"],
            "candidate_text_content_unverified": cc["text_content"],
            "reference_undeclared_elements": rc["undeclared_elements"],
            "reference_undeclared_attributes": rc["undeclared_attributes"],
            "reference_namespaced_attributes_unverified": rc["namespaced_attributes"],
            "reference_unverified_namespaces": rc["unverified_namespaces"],
            "reference_text_content_unverified": rc["text_content"],
            "unsupported_schema_constructs": unsupported,
        },
        "structure_quantity_coverage": _quantity_coverage(cand_root, ref_root, declared),
        "field_value_fidelity": fid,
        "note": "stdlib check: XSD attribute-vocabulary + hierarchical record/value diff, fail-closed on "
                "anything unverifiable. Full XSD content-model validation requires lxml (optional, not installed).",
    }


def _valid_threshold(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a real number in [0, 1], got {value!r}")
    if math.isnan(value) or math.isinf(value) or not (0.0 <= value <= 1.0):
        raise ValueError(f"{name} must be finite and in [0, 1], got {value!r}")


def evaluate(report: dict, min_element_coverage: float = 0.9,
             min_attribute_coverage: float = 0.9, allow_extra: bool = False) -> list[str]:
    """Return failure reasons; empty means the candidate passes the strict gate.

    Fails closed: any unverifiable feature (unknown schema construct, foreign
    namespace, namespaced attribute, text content) counts as a failure, on either
    side, because the harness cannot certify what it cannot check.
    """
    _valid_threshold("min_element_coverage", min_element_coverage)
    _valid_threshold("min_attribute_coverage", min_attribute_coverage)

    reasons: list[str] = []
    conf = report["xsd_conformance"]
    if conf["unsupported_schema_constructs"]:
        reasons.append(f"schema uses constructs this check cannot verify: {conf['unsupported_schema_constructs']}")
    for side in ("candidate", "reference"):
        if conf[f"{side}_unverified_namespaces"]:
            reasons.append(f"{side} uses namespaces the XSD does not target (unverified): {conf[f'{side}_unverified_namespaces']}")
        if conf[f"{side}_namespaced_attributes_unverified"]:
            reasons.append(f"{side} has namespaced attributes not supported by the schema (unverified): {conf[f'{side}_namespaced_attributes_unverified']}")
        if conf[f"{side}_text_content_unverified"]:
            reasons.append(f"{side} has element text content this check does not verify (unverified): {conf[f'{side}_text_content_unverified']}")
        if conf[f"{side}_undeclared_elements"]:
            reasons.append(f"{side} emits undeclared elements: {conf[f'{side}_undeclared_elements']}")
        if conf[f"{side}_undeclared_attributes"]:
            reasons.append(f"{side} emits undeclared attributes: {conf[f'{side}_undeclared_attributes']}")

    cov = report["structure_quantity_coverage"]
    if cov["missing_element_types"]:
        reasons.append(f"reference element types absent from candidate: {cov['missing_element_types']}")

    fid = report["field_value_fidelity"]
    if fid["value_changes"]:
        reasons.append(f"{len(fid['value_changes'])} attribute value change(s) vs reference")
    if fid["missing_records"]:
        reasons.append(f"{len(fid['missing_records'])} missing record(s)")
    if fid["duplicate_records"]:
        reasons.append(f"{len(fid['duplicate_records'])} duplicate record(s) by identity")
    if fid["missing_values"]:
        reasons.append(f"{len(fid['missing_values'])} value(s) present in reference but empty/absent in candidate")
    if fid["unverified_groups"]:
        reasons.append(f"{len(fid['unverified_groups'])} record group(s) without reliable identity — unverified, not passed")
    if not allow_extra:
        if fid["extra_records"]:
            reasons.append(f"{len(fid['extra_records'])} extra record(s) not in reference (use an explicit policy to allow)")
        if fid["extra_values"]:
            reasons.append(f"{len(fid['extra_values'])} extra filled value(s) not in reference (use an explicit policy to allow)")

    if cov["element_ratio"] < min_element_coverage:
        reasons.append(f"element coverage {cov['element_ratio']:.1%} < {min_element_coverage:.0%}")
    if cov["attribute_ratio"] < min_attribute_coverage:
        reasons.append(f"filled-attribute coverage {cov['attribute_ratio']:.1%} < {min_attribute_coverage:.0%}")
    return reasons


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="MFD->XML acceptance harness (structure coverage + hierarchical value fidelity)")
    p.add_argument("candidate")
    p.add_argument("reference")
    p.add_argument("xsd")
    p.add_argument("--min-element-coverage", type=float, default=0.9)
    p.add_argument("--min-attribute-coverage", type=float, default=0.9)
    p.add_argument("--allow-extra", action="store_true",
                   help="explicit policy: do not fail on extra records/values not present in the reference")
    p.add_argument("--strict", action="store_true",
                   help="exit non-zero on any conformance/fidelity failure or below-threshold coverage")
    args = p.parse_args(argv)
    report = compare(args.candidate, args.reference, args.xsd)
    try:
        reasons = evaluate(report, args.min_element_coverage, args.min_attribute_coverage, args.allow_extra)
    except ValueError as exc:
        print(f"invalid threshold: {exc}", file=sys.stderr)
        return 2
    report["gate"] = {"passed": not reasons, "reasons": reasons}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict and reasons:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
