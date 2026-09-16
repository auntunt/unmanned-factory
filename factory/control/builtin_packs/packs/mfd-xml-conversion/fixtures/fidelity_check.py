#!/usr/bin/env python3
"""Acceptance harness for MFD -> XML conversion. It does NOT convert anything.

Given a candidate XML (a converter's output), a reference XML (a trusted conversion
of the SAME MFD), and the Jiangsu-standard XSD, it produces three clearly separated
kinds of result, using only the Python standard library:

  1. xsd_conformance — every element and attribute the candidate emits must be a
     name the XSD declares *for that element itself*. Attributes declared on a
     nested child do not count for the parent. Elements in a namespace the XSD does
     not target are marked unverified, never silently accepted. Unsupported schema
     constructs (attribute groups, attribute refs) are reported as unverified, not
     assumed valid.

  2. structure_quantity_coverage — how many element instances and filled attributes
     the candidate carries relative to the reference. This is a *quantity* signal
     only. High coverage does NOT mean the values are correct; a candidate can hit
     100% of the counts while carrying wrong values or duplicated records. Read it
     alongside field_value_fidelity, never as a fidelity result on its own.

  3. field_value_fidelity — records aligned by a reliable business identity
     (an ``*ID`` / 编号 / 名称 / 序号 attribute that is present and unique in the
     group), then compared value by value. It reports value changes, missing values,
     missing records, duplicate records and extra records. When a group has no
     reliable identity (and is not an unambiguous singleton) it is marked
     *unverified* rather than passed. Numeric values are normalised with Decimal so
     "100", "100.0" and "100.00" compare equal; non-finite numbers (NaN/Inf) never
     compare equal.

Full XSD content-model validation (element order and occurrence) requires lxml,
which is optional and not a declared dependency of this repo; this harness never
claims that validation unless lxml is actually present.

CLI:
    python fidelity_check.py CANDIDATE.xml REFERENCE.xml STANDARD.xsd \
        [--min-element-coverage 0.9] [--min-attribute-coverage 0.9] [--strict]

With --strict the exit code is non-zero when the candidate emits undeclared or
unverified names, when the reference uses element types the candidate never emits,
when any value change / missing record / duplicate record / unverified group is
found, or when coverage is below the (validated, in [0,1], non-NaN) thresholds.
Extra or duplicated candidate records never offset missing ones.
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


def _qn(tag: str):
    """Return (namespace_uri_or_None, local_name), preserving the namespace."""
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return ns, local
    return None, tag


def _local(tag: str) -> str:
    return _qn(tag)[1]


# --------------------------------------------------------------------------- XSD

def parse_xsd(path: str):
    """Return (target_namespace, {element_local_name: set(own attribute names)},
    unsupported_constructs).

    An element's *own* attributes are the ``xs:attribute`` declarations reachable
    inside its ``xs:complexType`` without crossing into a nested ``xs:element`` —
    so a parent never inherits a child's attributes.
    """
    root = ET.parse(path).getroot()
    target_ns = root.get("targetNamespace")
    declared: dict[str, set[str]] = {}
    unsupported: list[str] = []

    def own_attributes(element) -> set[str]:
        names: set[str] = set()

        def walk(node, at_top):
            for child in node:
                lt = _local(child.tag)
                if lt == "element" and not at_top:
                    continue  # boundary: this attribute set belongs to the nested element
                if lt == "element" and at_top:
                    continue  # the element node itself is passed in as `element`, children are nested
                if lt == "attribute":
                    name = child.get("name")
                    if name:
                        names.add(name)
                    elif child.get("ref"):
                        unsupported.append(f"attribute ref on element {element.get('name')!r}")
                    continue
                if lt == "attributeGroup":
                    unsupported.append(f"attributeGroup on element {element.get('name')!r}")
                    continue
                walk(child, False)

        ctype = element.find(_XS + "complexType")
        if ctype is not None:
            walk(ctype, at_top=True)
        return names

    for el in root.iter(_XS + "element"):
        name = el.get("name")
        if not name:
            continue
        declared.setdefault(name, set()).update(own_attributes(el))
    return target_ns, declared, sorted(set(unsupported))


# ----------------------------------------------------------------------- records

def _records(path: str):
    """Flatten a document into records carrying their ancestor path and attributes.

    Each record: {"path": ((ns, local), ...) incl. self, "q": (ns, local),
    "attrs": {attr_local: value}}. Attribute keys use their local name
    (attributeFormDefault is unqualified in this schema).
    """
    root = ET.parse(path).getroot()
    out: list[dict] = []

    def walk(elem, ancestors):
        q = _qn(elem.tag)
        here = ancestors + (q,)
        attrs = {_local(k): v for k, v in elem.attrib.items()}
        out.append({"path": here, "q": q, "attrs": attrs})
        for child in elem:
            walk(child, here)

    walk(root, ())
    return out


# ------------------------------------------------------------------ conformance

def _conformance(records, declared, target_ns):
    undeclared_elems, undeclared_attrs, unverified_ns = set(), set(), set()
    for rec in records:
        ns, local = rec["q"]
        if ns != target_ns:
            unverified_ns.add(f"{{{ns}}}{local}" if ns else local)
            continue  # cannot judge a name from a namespace the XSD does not target
        if local not in declared:
            undeclared_elems.add(local)
            continue
        for attr in rec["attrs"]:
            if attr not in declared[local]:
                undeclared_attrs.add((local, attr))
    return (sorted(undeclared_elems),
            [list(p) for p in sorted(undeclared_attrs)],
            sorted(unverified_ns))


# ----------------------------------------------------------------- value helpers

def _num(value):
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _values_equal(a, b) -> bool:
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return na == nb
    return str(a).strip() == str(b).strip()


_ID_FALLBACKS = ("编号", "名称", "id", "序号")


def _choose_key(records):
    """Pick an attribute that is present and unique across the group, preferring an
    ``*ID`` name, else 编号/名称/id/序号. Returns None when none is reliable."""
    if not records:
        return None
    attrs = set().union(*[set(r["attrs"]) for r in records])

    def unique(attr):
        vals = [r["attrs"].get(attr, "").strip() for r in records]
        return all(vals) and len(set(vals)) == len(vals)

    for attr in sorted(a for a in attrs if a.endswith("ID")):
        if unique(attr):
            return attr
    for attr in _ID_FALLBACKS:
        if attr in attrs and unique(attr):
            return attr
    return None


def _compare_values(loc, ref_attrs, cand_attrs, key_desc, result):
    for attr, rv in ref_attrs.items():
        if rv is None or str(rv).strip() == "":
            continue
        cv = cand_attrs.get(attr)
        entry = {"location": loc, "attr": attr}
        if key_desc:
            entry["key"] = key_desc
        if cv is None or str(cv).strip() == "":
            result["missing_values"].append(entry)
        elif not _values_equal(rv, cv):
            result["value_changes"].append({**entry, "reference": rv, "candidate": cv})


def align_and_diff(candidate, reference):
    """Align records by structural path and reliable identity; report differences."""
    def group(records):
        g = defaultdict(list)
        for r in records:
            g[r["path"]].append(r)
        return g

    gc, gr = group(candidate), group(reference)
    result = {"value_changes": [], "missing_values": [], "missing_records": [],
              "extra_records": [], "duplicate_records": [], "unverified_groups": []}

    for path, rrecs in gr.items():
        crecs = gc.get(path, [])
        loc = "/".join(local for _, local in path)
        key = _choose_key(rrecs)
        cand_has_key = bool(key) and all(r["attrs"].get(key, "").strip() for r in crecs)

        if not key or not cand_has_key:
            if len(rrecs) == 1 and len(crecs) == 1:
                # Unambiguous singleton: positional alignment is reliable.
                _compare_values(loc, rrecs[0]["attrs"], crecs[0]["attrs"], None, result)
                continue
            if len(crecs) < len(rrecs):
                result["missing_records"].append(
                    {"location": loc, "missing_count": len(rrecs) - len(crecs), "unverified": True})
            elif len(crecs) > len(rrecs):
                result["extra_records"].append(
                    {"location": loc, "extra_count": len(crecs) - len(rrecs), "unverified": True})
            result["unverified_groups"].append({
                "location": loc, "reference_count": len(rrecs), "candidate_count": len(crecs),
                "reason": "no reliable identity attribute" if not key else "candidate lacks the identity attribute"})
            continue

        rk = Counter(r["attrs"].get(key, "").strip() for r in rrecs)
        ck = Counter(r["attrs"].get(key, "").strip() for r in crecs)
        for k, c in ck.items():
            if c > 1:
                result["duplicate_records"].append({"location": loc, "key": f"{key}={k}", "count": c, "side": "candidate"})
        for k, c in rk.items():
            if c > 1:
                result["duplicate_records"].append({"location": loc, "key": f"{key}={k}", "count": c, "side": "reference"})
        rmap = {r["attrs"].get(key, "").strip(): r for r in rrecs}
        cmap = {r["attrs"].get(key, "").strip(): r for r in crecs}
        for k in rmap:
            if k not in cmap:
                result["missing_records"].append({"location": loc, "key": f"{key}={k}"})
        for k in cmap:
            if k not in rmap:
                result["extra_records"].append({"location": loc, "key": f"{key}={k}"})
        for k in rmap:
            if k in cmap:
                _compare_values(loc, rmap[k]["attrs"], cmap[k]["attrs"], f"{key}={k}", result)

    for path, crecs in gc.items():
        if path not in gr:
            loc = "/".join(local for _, local in path)
            result["extra_records"].append({"location": loc, "extra_count": len(crecs), "not_in_reference": True})

    for name, items in result.items():
        if len(items) > _MAX_DETAIL:
            result[name] = items[:_MAX_DETAIL] + [{"_truncated": len(items) - _MAX_DETAIL}]
    return result


# --------------------------------------------------------------------- assembly

def _quantity_coverage(candidate, reference, declared):
    ct = Counter(r["q"][1] for r in candidate)
    rt = Counter(r["q"][1] for r in reference)

    def filled(records):
        n = 0
        for r in records:
            n += sum(1 for v in r["attrs"].values() if v is not None and str(v).strip() != "")
        return n

    cand_elems, ref_elems = len(candidate), len(reference)
    cand_filled, ref_filled = filled(candidate), filled(reference)

    def ratio(a, b):
        return round(a / b, 4) if b else (1.0 if a == 0 else 0.0)

    missing = sorted(t for t in rt if t in declared and rt[t] > 0 and ct.get(t, 0) == 0)
    extra = sorted(t for t in ct if t in declared and ct[t] > 0 and rt.get(t, 0) == 0)
    return {
        "candidate_element_total": cand_elems,
        "reference_element_total": ref_elems,
        "element_ratio": ratio(cand_elems, ref_elems),
        "candidate_filled_attribute_total": cand_filled,
        "reference_filled_attribute_total": ref_filled,
        "attribute_ratio": ratio(cand_filled, ref_filled),
        "missing_element_types": missing,
        "extra_element_types": extra,
        "caveat": "quantity/structure signal only; NOT a field-value fidelity result.",
    }


def compare(candidate: str, reference: str, xsd_path: str) -> dict:
    target_ns, declared, unsupported = parse_xsd(xsd_path)
    cand = _records(candidate)
    ref = _records(reference)

    cue, cua, cun = _conformance(cand, declared, target_ns)
    rue, rua, run_ = _conformance(ref, declared, target_ns)

    return {
        "xsd_declared_elements": len(declared),
        "xsd_target_namespace": target_ns,
        "xsd_conformance": {
            "candidate_undeclared_elements": cue,
            "candidate_undeclared_attributes": cua,
            "candidate_unverified_namespaces": cun,
            "reference_undeclared_elements": rue,
            "reference_undeclared_attributes": rua,
            "reference_unverified_namespaces": run_,
            "unsupported_schema_constructs": unsupported,
        },
        "structure_quantity_coverage": _quantity_coverage(cand, ref, declared),
        "field_value_fidelity": align_and_diff(cand, ref),
        "note": "stdlib check: XSD attribute-vocabulary + record-level value diff. "
                "Full XSD content-model validation requires lxml (optional, not installed by default).",
    }


def _valid_threshold(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a real number in [0, 1], got {value!r}")
    if math.isnan(value) or math.isinf(value) or not (0.0 <= value <= 1.0):
        raise ValueError(f"{name} must be in [0, 1] and finite, got {value!r}")


def evaluate(report: dict, min_element_coverage: float = 0.9,
             min_attribute_coverage: float = 0.9) -> list[str]:
    """Return failure reasons; empty means the candidate passes the strict gate."""
    _valid_threshold("min_element_coverage", min_element_coverage)
    _valid_threshold("min_attribute_coverage", min_attribute_coverage)

    reasons: list[str] = []
    conf = report["xsd_conformance"]
    if conf["candidate_undeclared_elements"]:
        reasons.append(f"candidate emits undeclared elements: {conf['candidate_undeclared_elements']}")
    if conf["candidate_undeclared_attributes"]:
        reasons.append(f"candidate emits undeclared attributes: {conf['candidate_undeclared_attributes']}")
    if conf["candidate_unverified_namespaces"]:
        reasons.append(f"candidate uses namespaces the XSD does not target (unverified): {conf['candidate_unverified_namespaces']}")
    if conf["unsupported_schema_constructs"]:
        reasons.append(f"schema uses constructs this stdlib check cannot verify: {conf['unsupported_schema_constructs']}")

    cov = report["structure_quantity_coverage"]
    if cov["missing_element_types"]:
        reasons.append(f"reference element types absent from candidate: {cov['missing_element_types']}")

    fid = report["field_value_fidelity"]
    if fid["value_changes"]:
        reasons.append(f"{len(fid['value_changes'])} attribute value change(s) vs reference")
    if fid["missing_records"]:
        reasons.append(f"{len(fid['missing_records'])} missing record group(s)/record(s)")
    if fid["duplicate_records"]:
        reasons.append(f"{len(fid['duplicate_records'])} duplicate record(s) by identity")
    if fid["missing_values"]:
        reasons.append(f"{len(fid['missing_values'])} attribute value(s) present in reference but empty/absent in candidate")
    if fid["unverified_groups"]:
        reasons.append(f"{len(fid['unverified_groups'])} record group(s) without reliable identity — unverified, not passed")

    if cov["element_ratio"] < min_element_coverage:
        reasons.append(f"element coverage {cov['element_ratio']:.1%} < {min_element_coverage:.0%}")
    if cov["attribute_ratio"] < min_attribute_coverage:
        reasons.append(f"filled-attribute coverage {cov['attribute_ratio']:.1%} < {min_attribute_coverage:.0%}")
    return reasons


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="MFD->XML acceptance harness (structure coverage + value fidelity)")
    p.add_argument("candidate")
    p.add_argument("reference")
    p.add_argument("xsd")
    p.add_argument("--min-element-coverage", type=float, default=0.9)
    p.add_argument("--min-attribute-coverage", type=float, default=0.9)
    p.add_argument("--strict", action="store_true",
                   help="exit non-zero on any conformance/fidelity failure or below-threshold coverage")
    args = p.parse_args(argv)
    report = compare(args.candidate, args.reference, args.xsd)
    try:
        reasons = evaluate(report, args.min_element_coverage, args.min_attribute_coverage)
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
