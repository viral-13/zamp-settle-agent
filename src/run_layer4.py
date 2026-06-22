"""Settle — Layer 4 (Liability Classification) entrypoint. The judgment step.

`python src/run_layer4.py` does everything:
  1. ensure out/case_layer3.json exists (generate prior layers if missing),
  2. for each discrepancy, make ONE Sonnet call to classify cause + liable party,
  3. fill each discrepancy.classification (numbers untouched; decision left empty),
  4. write out/case_layer4.json (+ audit entries),
  5. print a PASS/FAIL table, cross-checking the model output against ground_truth
     (read separately here — NEVER fed to the model).
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

from classify.liability import classify_discrepancy, CLASSIFIER_MODEL  # noqa: E402

DATA = ROOT / "data"
OUT = ROOT / "out"
CONFIG = ROOT / "config" / "config.json"

# Matches config.confidence_routing.auto_act_threshold (Layer 5 will route on it).
AUTO_ACT_THRESHOLD = 0.80


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_layer3() -> dict:
    case_path = OUT / "case_layer3.json"
    if not case_path.exists():
        print("out/case_layer3.json missing — running Layer 3 (and prior) to generate it…\n")
        import run_layer3
        if run_layer3.main() != 0:
            raise RuntimeError("Layer 3 failed; cannot continue Layer 4.")
        print()
    return json.loads(case_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# classification pipeline
# --------------------------------------------------------------------------- #
def run_classification() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    case = _ensure_layer3()
    evidence = case["evidence"]

    print(f"Classifying {len(case['discrepancies'])} discrepancies via {CLASSIFIER_MODEL} "
          f"(one call each)…\n")
    audit = case.get("audit", [])
    for d in case["discrepancies"]:
        qty_before, value_before = d["qty"], d["value_usd"]  # snapshot to prove untouched
        cls = classify_discrepancy(d, evidence, config)

        # numbers are fixed by Layer 3 — guard that nothing drifted.
        assert d["qty"] == qty_before and d["value_usd"] == value_before, "Layer 4 must not touch numbers"

        d["classification"] = cls
        # d["decision"] stays {} — Layer 5.
        print(f"  {d['type']:<8} -> cause={cls['cause']}, liable={cls['liable_party']}, "
              f"confidence={cls['confidence']}")
        audit.append({
            "ts": _now(),
            "step": "layer4.classify",
            "detail": (f"{d['type']} [{d['discrepancy_id']}] model={CLASSIFIER_MODEL}: "
                       f"cause={cls['cause']}, liable_party={cls['liable_party']}, "
                       f"confidence={cls['confidence']} — {cls['rationale']}"),
        })
    case["audit"] = audit
    # payable stays {} — Layer 5.

    out_path = OUT / "case_layer4.json"
    out_path.write_text(json.dumps(case, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path.relative_to(ROOT)}\n")
    return case


# --------------------------------------------------------------------------- #
# verification (ground_truth read here only — never sent to the model)
# --------------------------------------------------------------------------- #
def _print_table(rows: list[tuple[str, str, str]]) -> bool:
    name_w = max(len(r[0]) for r in rows)
    detail_w = max(len(r[1]) for r in rows)
    print(f"  {'CHECK'.ljust(name_w)}   {'DETAIL'.ljust(detail_w)}  RESULT")
    print("  " + "-" * (name_w + detail_w + 12))
    for name, detail, result in rows:
        print(f"  {name.ljust(name_w)}   {detail.ljust(detail_w)}  {result}")
    return all(r[2] == "PASS" for r in rows)


def verify(case: dict, layer3_case: dict) -> bool:
    print("Layer 4 verification (ground_truth read separately, never fed to the model):\n")
    by_type = {d["type"]: d for d in case["discrepancies"]}
    dmg = by_type["damaged"]
    short = by_type["short"]

    gt = json.loads((DATA / "ground_truth.json").read_text(encoding="utf-8"))
    gt_by_type = {d["type"]: d for d in gt["expected_discrepancies"]}

    dmg_cls = dmg["classification"]
    short_cls = short["classification"]

    # damaged: carrier / in_transit_damage / high band (>= 0.80)
    dmg_ok = (
        dmg_cls["liable_party"] == "carrier"
        and dmg_cls["cause"] == "in_transit_damage"
        and dmg_cls["confidence"] >= AUTO_ACT_THRESHOLD
        and dmg_cls["liable_party"] == gt_by_type["damaged"]["liable_party"]
        and dmg_cls["cause"] == gt_by_type["damaged"]["cause"]
    )
    # short: supplier / supplier_short_load / low band (< 0.80)
    short_ok = (
        short_cls["liable_party"] == "supplier"
        and short_cls["cause"] == "supplier_short_load"
        and short_cls["confidence"] < AUTO_ACT_THRESHOLD
        and short_cls["liable_party"] == gt_by_type["short"]["liable_party"]
        and short_cls["cause"] == gt_by_type["short"]["cause"]
    )

    # citations present + reference real evidence keys
    evidence_keys = set(case["evidence"].keys())

    def _cites_real(cls: dict) -> bool:
        cited = cls.get("evidence_cited", [])
        if not cited:
            return False
        joined = " ".join(cited).lower()
        return any(k.lower() in joined for k in evidence_keys)

    citations_ok = _cites_real(dmg_cls) and _cites_real(short_cls)

    # numbers untouched vs Layer 3
    l3_by_type = {d["type"]: d for d in layer3_case["discrepancies"]}
    numbers_ok = all(
        by_type[t]["qty"] == l3_by_type[t]["qty"] and by_type[t]["value_usd"] == l3_by_type[t]["value_usd"]
        for t in ("damaged", "short")
    )

    # boundary — no Layer 5 work leaked in
    boundary_ok = all(d["decision"] == {} for d in case["discrepancies"]) and case["payable"] == {}

    rows = [
        ("damaged_classification",
         f"liable={dmg_cls['liable_party']}, cause={dmg_cls['cause']}, conf={dmg_cls['confidence']} (>=0.80)",
         "PASS" if dmg_ok else "FAIL"),
        ("short_classification",
         f"liable={short_cls['liable_party']}, cause={short_cls['cause']}, conf={short_cls['confidence']} (<0.80)",
         "PASS" if short_ok else "FAIL"),
        ("citations_present",
         f"damaged={len(dmg_cls['evidence_cited'])} cites, short={len(short_cls['evidence_cited'])} cites",
         "PASS" if citations_ok else "FAIL"),
        ("numbers_untouched", "qty/value_usd unchanged from Layer 3",
         "PASS" if numbers_ok else "FAIL"),
        ("boundary_integrity", "decision empty, payable=={}",
         "PASS" if boundary_ok else "FAIL"),
    ]
    all_pass = _print_table(rows)
    print(f"\n  LAYER 4 VERIFICATION: {'PASS ✓' if all_pass else 'FAIL ✗'}")
    return all_pass


def main() -> int:
    layer3_case = _ensure_layer3()
    case = run_classification()
    ok = verify(case, layer3_case)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
