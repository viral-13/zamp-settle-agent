"""Settle — Layer 2 (Normalize & Resolve) entrypoint.

`python src/run_layer2.py` does everything:
  1. ensure out/case_layer1.json exists (generate via Layer 1 if missing),
  2. read the supplier item code from the ASN (reusing the Layer 1 parser),
  3. resolve identity against the maintained mapping table (source of truth),
  4. run deterministic UoM cross-checks,
  5. compose out/case_layer2.json (adds a `resolution` block + audit entries),
  6. print a PASS/FAIL verification table, then run the unknown-code unit test.

In the locked scenario the supplier code is known, so this makes ZERO model calls.
"""

from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import json
import sys
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ingest.parsers import parse_asn_856  # noqa: E402  (reuse Layer 1 parser)
from normalize.resolve import load_config, resolve_item, uom_checks  # noqa: E402

DATA = ROOT / "data"
OUT = ROOT / "out"
CONFIG = ROOT / "config" / "config.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_layer1() -> dict:
    """Load out/case_layer1.json, regenerating it via Layer 1 if missing."""
    case_path = OUT / "case_layer1.json"
    if not case_path.exists():
        print("out/case_layer1.json missing — running Layer 1 to generate it…\n")
        import run_layer1
        if run_layer1.main() != 0:
            raise RuntimeError("Layer 1 failed; cannot continue Layer 2.")
        print()
    return json.loads(case_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# normalization pipeline
# --------------------------------------------------------------------------- #
def run_normalize() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    config = load_config(CONFIG)
    case = _ensure_layer1()

    # 1) entity resolution — supplier code comes from the ASN (reuse Layer 1 parser)
    asn = parse_asn_856(DATA / "asn_856_raw.edi")
    supplier_code = asn["lines"][0]["supplier_item_code"]
    mapping = resolve_item(supplier_code, config)  # suggest=False -> no model call

    # consistency check: resolved SKU must match the SKU already on the case po_line
    po_sku = case["po_line"]["sku"]
    if mapping["resolved_sku"] is not None and mapping["resolved_sku"] != po_sku:
        raise AssertionError(
            f"Resolved SKU {mapping['resolved_sku']!r} != po_line.sku {po_sku!r}"
        )

    # 2) deterministic UoM cross-checks
    checks = uom_checks(case["evidence"], config)

    # 3) compose the resolution block + audit (data only, no UI)
    case["resolution"] = {
        "item_mappings": [mapping],
        "uom_checks": checks,
    }
    audit = case.get("audit", [])
    audit.append({
        "ts": _now(),
        "step": "layer2.resolve_item",
        "detail": (f"{supplier_code} -> {mapping['resolved_sku']} via {mapping['method']} "
                   f"(routing={mapping['routing']}, source={mapping['source']}); "
                   f"consistent with po_line.sku={po_sku}"),
    })
    for chk in checks:
        audit.append({
            "ts": _now(),
            "step": "layer2.uom_check",
            "detail": f"{chk['check']} -> expected {chk['expected']}, actual {chk['actual']}: {chk['result']}",
        })
    case["audit"] = audit

    out_path = OUT / "case_layer2.json"
    out_path.write_text(json.dumps(case, indent=2), encoding="utf-8")
    print(f"Wrote {out_path.relative_to(ROOT)} (zero model calls — table lookup)\n")
    return case


# --------------------------------------------------------------------------- #
# verification table
# --------------------------------------------------------------------------- #
def _print_table(rows: list[tuple[str, str, str]]) -> bool:
    name_w = max(len(r[0]) for r in rows)
    detail_w = max(len(r[1]) for r in rows)
    all_pass = all(r[2] == "PASS" for r in rows)
    print(f"  {'CHECK'.ljust(name_w)}   {'DETAIL'.ljust(detail_w)}  RESULT")
    print("  " + "-" * (name_w + detail_w + 12))
    for name, detail, result in rows:
        print(f"  {name.ljust(name_w)}   {detail.ljust(detail_w)}  {result}")
    return all_pass


def verify(case: dict) -> bool:
    print("Layer 2 verification:\n")
    mapping = case["resolution"]["item_mappings"][0]
    checks = {c["check"].split("(")[0].split(" ==")[0].strip(): c for c in case["resolution"]["uom_checks"]}
    po_sku = case["po_line"]["sku"]

    # item_resolution
    item_ok = (
        mapping["supplier_code"] == "SUP-4471"
        and mapping["resolved_sku"] == "SKU-A"
        and mapping["method"] == "table"
        and mapping["routing"] == "auto"
        and mapping["resolved_sku"] == po_sku
    )

    # uom_crosscheck (the 20*25==500 check)
    uom = case["resolution"]["uom_checks"][0]
    uom_ok = uom["result"] == "PASS" and uom["actual"] == case["evidence"]["shipped_qty"]

    # case_integrity — no later-layer work leaked in
    integrity_ok = case["discrepancies"] == [] and case["payable"] == {}

    rows = [
        ("item_resolution",
         f"SUP-4471 -> {mapping['resolved_sku']}, method={mapping['method']}, routing={mapping['routing']}, ==po_line.sku",
         "PASS" if item_ok else "FAIL"),
        ("uom_crosscheck",
         f"20 pallets x 25 = {uom['actual']} == shipped_qty {case['evidence']['shipped_qty']}",
         "PASS" if uom_ok else "FAIL"),
        ("case_integrity",
         "discrepancies==[] and payable=={}",
         "PASS" if integrity_ok else "FAIL"),
    ]
    all_pass = _print_table(rows)
    print(f"\n  LAYER 2 VERIFICATION: {'PASS ✓' if all_pass else 'FAIL ✗'}")
    return all_pass


# --------------------------------------------------------------------------- #
# unit test — unknown code must escalate, never auto-apply (no model call)
# --------------------------------------------------------------------------- #
def unit_test_unknown_code() -> bool:
    print("\nUnit test — unknown code routing (no model call):\n")
    config = load_config(CONFIG)
    res = resolve_item("SUP-9999", config)  # suggest=False -> offline
    ok = res["routing"] == "escalate" and res["resolved_sku"] is None
    rows = [
        ("unknown_routes_escalate", f"SUP-9999 routing={res['routing']}",
         "PASS" if res["routing"] == "escalate" else "FAIL"),
        ("unknown_not_applied", f"resolved_sku={res['resolved_sku']!r} (not auto-applied)",
         "PASS" if res["resolved_sku"] is None else "FAIL"),
    ]
    all_pass = _print_table(rows)
    print(f"\n  UNKNOWN-CODE UNIT TEST: {'PASS ✓' if all_pass else 'FAIL ✗'}")
    return all_pass and ok


def main() -> int:
    case = run_normalize()
    v_ok = verify(case)
    t_ok = unit_test_unknown_code()
    return 0 if (v_ok and t_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
