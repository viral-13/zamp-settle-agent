"""Settle — Layer 3 (Reconciliation Engine) entrypoint.

`python src/run_layer3.py` does everything:
  1. ensure out/case_layer2.json exists (generate prior layers if missing),
  2. build the discrepancies[] facts (damaged / short) — deterministic,
  3. prove the books close two independent ways,
  4. compose out/case_layer3.json (discrepancies + reconciliation + audit),
  5. print a PASS/FAIL table and cross-check qty/value against ground_truth.

100% deterministic — no LLM, no API key needed.
"""

from pathlib import Path

import json
import sys
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reconcile.engine import build_discrepancies, reconcile  # noqa: E402

DATA = ROOT / "data"
OUT = ROOT / "out"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_layer2() -> dict:
    """Load out/case_layer2.json, regenerating prior layers if missing."""
    case_path = OUT / "case_layer2.json"
    if not case_path.exists():
        print("out/case_layer2.json missing — running Layer 2 (and Layer 1) to generate it…\n")
        import run_layer2
        if run_layer2.main() != 0:
            raise RuntimeError("Layer 2 failed; cannot continue Layer 3.")
        print()
    return json.loads(case_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# reconciliation pipeline
# --------------------------------------------------------------------------- #
def run_reconcile() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    case = _ensure_layer2()

    evidence = case["evidence"]
    po_line = case["po_line"]

    discrepancies = build_discrepancies(evidence, po_line)
    reconciliation = reconcile(evidence, po_line)  # raises loudly if books don't close

    case["discrepancies"] = discrepancies
    case["reconciliation"] = reconciliation
    # payable stays {} — Layer 5 owns it.

    audit = case.get("audit", [])
    for d in discrepancies:
        audit.append({
            "ts": _now(),
            "step": "layer3.discrepancy",
            "detail": f"{d['type']}: qty={d['qty']}, value_usd={d['value_usd']} "
                      f"(= qty * unit_price {po_line['unit_price']})",
        })
    for chk in reconciliation["checks"]:
        audit.append({
            "ts": _now(),
            "step": "layer3.closure_check",
            "detail": f"{chk['check']} -> expected {chk['expected']}, actual {chk['actual']}: {chk['result']}",
        })
    case["audit"] = audit

    out_path = OUT / "case_layer3.json"
    out_path.write_text(json.dumps(case, indent=2), encoding="utf-8")
    print(f"Wrote {out_path.relative_to(ROOT)} (deterministic — no model calls)\n")
    return case


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #
def _print_table(rows: list[tuple[str, str, str]]) -> bool:
    name_w = max(len(r[0]) for r in rows)
    detail_w = max(len(r[1]) for r in rows)
    print(f"  {'CHECK'.ljust(name_w)}   {'DETAIL'.ljust(detail_w)}  RESULT")
    print("  " + "-" * (name_w + detail_w + 12))
    for name, detail, result in rows:
        print(f"  {name.ljust(name_w)}   {detail.ljust(detail_w)}  {result}")
    return all(r[2] == "PASS" for r in rows)


def verify(case: dict) -> bool:
    print("Layer 3 verification:\n")
    by_type = {d["type"]: d for d in case["discrepancies"]}
    dmg = by_type["damaged"]
    short = by_type["short"]
    recon = case["reconciliation"]

    # ground-truth cross-check on qty + value_usd ONLY (no cause/liability/routing here).
    gt = json.loads((DATA / "ground_truth.json").read_text(encoding="utf-8"))
    gt_by_type = {d["type"]: d for d in gt["expected_discrepancies"]}

    dmg_ok = (dmg["qty"] == 25 and dmg["value_usd"] == 1000
              and dmg["qty"] == gt_by_type["damaged"]["qty"]
              and dmg["value_usd"] == gt_by_type["damaged"]["value_usd"])
    short_ok = (short["qty"] == 15 and short["value_usd"] == 600
                and short["qty"] == gt_by_type["short"]["qty"]
                and short["value_usd"] == gt_by_type["short"]["value_usd"])

    closes_ok = (recon["closes"] is True
                 and all(c["result"] == "PASS" for c in recon["checks"]))

    # boundary integrity — no Layer 4/5 work leaked in.
    boundary_ok = (all(d["classification"] == {} for d in case["discrepancies"])
                   and all(d["decision"] == {} for d in case["discrepancies"])
                   and case["payable"] == {})

    rows = [
        ("discrepancy_damaged", f"qty {dmg['qty']}, value_usd {dmg['value_usd']}",
         "PASS" if dmg_ok else "FAIL"),
        ("discrepancy_short", f"qty {short['qty']}, value_usd {short['value_usd']}",
         "PASS" if short_ok else "FAIL"),
        ("reconciliation_closes",
         f"{recon['good']}+{recon['damaged']}+{recon['short']}=={recon['ordered']} AND indep short==GRN",
         "PASS" if closes_ok else "FAIL"),
        ("boundary_integrity", "classification/decision empty, payable=={}",
         "PASS" if boundary_ok else "FAIL"),
    ]
    all_pass = _print_table(rows)
    print(f"\n  LAYER 3 VERIFICATION: {'PASS ✓' if all_pass else 'FAIL ✗'}")
    return all_pass


def main() -> int:
    case = run_reconcile()
    ok = verify(case)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
