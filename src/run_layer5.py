"""Settle — Layer 5 (Decision & Action) entrypoint. The final functional layer.

`python src/run_layer5.py` does everything:
  1. ensure out/case_layer4.json exists (generate prior layers if missing),
  2. deterministically gate each classified discrepancy (auto vs escalate) and pick
     its action from config,
  3. draft a structured claim/debit packet per discrepancy (+ optional Haiku cover
     note built only from packet facts), written to out/claims/,
  4. compute the payable position and emit the finance-handoff STUB,
  5. fill each discrepancy.decision + payable + handoff, write out/case_layer5.json,
  6. print a PASS/FAIL table cross-checked against ground_truth.
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

from ingest.parsers import parse_asn_856  # noqa: E402  (reuse Layer 1 parser for refs)
from decide.policy import route, select_action, compute_payable, build_handoff_stub  # noqa: E402
from decide.draft import build_packet, cover_note_invents_numbers  # noqa: E402

DATA = ROOT / "data"
OUT = ROOT / "out"
CLAIMS = OUT / "claims"
CONFIG = ROOT / "config" / "config.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_layer4() -> dict:
    case_path = OUT / "case_layer4.json"
    if not case_path.exists():
        print("out/case_layer4.json missing — running Layer 4 (and prior) to generate it…\n")
        import run_layer4
        if run_layer4.main() != 0:
            raise RuntimeError("Layer 4 failed; cannot continue Layer 5.")
        print()
    return json.loads(case_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# decision pipeline
# --------------------------------------------------------------------------- #
def run_decision() -> dict:
    CLAIMS.mkdir(parents=True, exist_ok=True)
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    case = _ensure_layer4()

    # Identifier refs (deterministic, from the ASN + config) — not computed numbers.
    asn = parse_asn_856(DATA / "asn_856_raw.edi")
    refs = {
        "claimant": "DCNORTH",  # buyer / receiving DC
        "carrier_scac": asn["carrier_scac"],
        "supplier_id": asn["supplier_id"],
        "bol_number": asn["bol_number"],
        "asn_number": asn["asn_number"],
    }

    audit = case.get("audit", [])
    claim_refs: list[str] = []

    print("Gating, drafting packets, and writing out/claims/…\n")
    for d in case["discrepancies"]:
        cls = d["classification"]
        routing = route(cls["confidence"], d["value_usd"], config)      # deterministic
        action = select_action(cls["cause"], config)                    # from config
        packet = build_packet(d, case, refs, action, routing)

        # persist the packet
        packet_path = CLAIMS / f"{packet['claim_ref']}.json"
        packet_path.write_text(json.dumps(packet, indent=2), encoding="utf-8")
        claim_refs.append(packet["claim_ref"])

        # fill the decision block (Layer 5 owns this)
        d["decision"] = {
            "action": action,
            "routing": routing,
            "claim_packet_ref": packet["claim_ref"],
        }
        print(f"  {d['type']:<8} -> action={action}, routing={routing}, "
              f"value=${d['value_usd']} -> {packet['status']} "
              f"(cover note: {packet['cover_note_source']})")
        audit.append({
            "ts": _now(),
            "step": "layer5.decision",
            "detail": (f"{d['type']} [{d['discrepancy_id']}] action={action}, routing={routing} "
                       f"(confidence={cls['confidence']}, value_usd={d['value_usd']}); "
                       f"packet={packet['claim_ref']} status={packet['status']}"),
        })

    # payable (deterministic) + finance handoff stub
    payable = compute_payable(case)
    case["payable"] = payable
    handoff = build_handoff_stub(case, payable, claim_refs)
    case["finance_handoff"] = handoff

    audit.append({
        "ts": _now(),
        "step": "layer5.payable",
        "detail": (f"invoice_expected={payable['invoice_expected_usd']}, "
                   f"payable_good={payable['payable_good_usd']}, "
                   f"leakage_caught={payable['leakage_caught_usd']}"),
    })
    audit.append({
        "ts": _now(),
        "step": "layer5.finance_handoff",
        "detail": (f"STUB handoff: payable_good={handoff['payable_good_usd']} pre-cleared against "
                   f"incoming 810 (qty={handoff['incoming_invoice_810_stub']['qty']}, "
                   f"value={handoff['incoming_invoice_810_stub']['value']}); no fresh AP exception. "
                   f"claims={claim_refs}"),
    })
    case["audit"] = audit
    if case.get("status") == "open":
        case["status"] = "concluded"

    out_path = OUT / "case_layer5.json"
    out_path.write_text(json.dumps(case, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path.relative_to(ROOT)} (final settled case)\n")
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


def _load_packet(claim_ref: str) -> dict:
    return json.loads((CLAIMS / f"{claim_ref}.json").read_text(encoding="utf-8"))


def verify(case: dict) -> bool:
    print("Layer 5 verification (cross-checked against ground_truth):\n")
    by_type = {d["type"]: d for d in case["discrepancies"]}
    dmg, short = by_type["damaged"], by_type["short"]
    dmg_pkt = _load_packet(dmg["decision"]["claim_packet_ref"])
    short_pkt = _load_packet(short["decision"]["claim_packet_ref"])
    payable = case["payable"]
    handoff = case["finance_handoff"]

    gt = json.loads((DATA / "ground_truth.json").read_text(encoding="utf-8"))
    gt_by_type = {d["type"]: d for d in gt["expected_discrepancies"]}
    gt_pay = gt["expected_payable"]

    # damaged decision
    dmg_ok = (
        dmg["decision"]["action"] == "draft_carrier_freight_claim"
        and dmg["decision"]["routing"] == "auto"
        and dmg_pkt["value_usd"] == 1000
        and dmg["decision"]["action"] == gt_by_type["damaged"]["action"]
        and dmg["decision"]["routing"] == gt_by_type["damaged"]["routing"]
    )
    # short decision (drafted but held for a human)
    short_ok = (
        short["decision"]["action"] == "draft_supplier_debit"
        and short["decision"]["routing"] == "escalate"
        and short_pkt["value_usd"] == 600
        and short_pkt["held_for_human"] is True
        and short["decision"]["action"] == gt_by_type["short"]["action"]
        and short["decision"]["routing"] == gt_by_type["short"]["routing"]
    )
    # payable
    payable_ok = (
        payable["invoice_expected_usd"] == 20000
        and payable["payable_good_usd"] == 18400
        and payable["leakage_caught_usd"] == 1600
        and payable["invoice_expected_usd"] == gt_pay["invoice_expected_usd"]
        and payable["payable_good_usd"] == gt_pay["payable_good_usd"]
        and payable["leakage_caught_usd"] == gt_pay["leakage_caught_usd"]
    )
    # no invented numbers in cover notes
    dmg_invents = cover_note_invents_numbers(dmg_pkt)
    short_invents = cover_note_invents_numbers(short_pkt)
    numbers_clean = not dmg_invents and not short_invents

    # finance handoff stub
    handoff_ok = (
        "STUB" in handoff.get("note", "")
        and handoff["status"] == "pre-cleared"
        and handoff["payable_good_usd"] == 18400
        and handoff["incoming_invoice_810_stub"]["value"] == 20000
    )
    # case completeness
    complete_ok = all(d["classification"] and d["decision"] for d in case["discrepancies"]) and bool(payable)

    # numbers untouched vs Layer 3
    l3 = json.loads((OUT / "case_layer3.json").read_text(encoding="utf-8"))
    l3_by_type = {d["type"]: d for d in l3["discrepancies"]}
    numbers_untouched = all(
        by_type[t]["qty"] == l3_by_type[t]["qty"] and by_type[t]["value_usd"] == l3_by_type[t]["value_usd"]
        for t in ("damaged", "short")
    )

    rows = [
        ("damaged_decision",
         f"action={dmg['decision']['action']}, routing={dmg['decision']['routing']}, value={dmg_pkt['value_usd']}",
         "PASS" if dmg_ok else "FAIL"),
        ("short_decision",
         f"action={short['decision']['action']}, routing={short['decision']['routing']}, value={short_pkt['value_usd']}, held={short_pkt['held_for_human']}",
         "PASS" if short_ok else "FAIL"),
        ("payable",
         f"invoice={payable['invoice_expected_usd']}, good={payable['payable_good_usd']}, leakage={payable['leakage_caught_usd']}",
         "PASS" if payable_ok else "FAIL"),
        ("no_invented_numbers",
         f"damaged invents={dmg_invents or 'none'}, short invents={short_invents or 'none'}",
         "PASS" if numbers_clean else "FAIL"),
        ("finance_handoff_stub",
         f"status={handoff['status']}, pre-cleared vs {handoff['payable_good_usd']}, labeled STUB",
         "PASS" if handoff_ok else "FAIL"),
        ("case_complete", "every discrepancy has classification+decision; payable filled",
         "PASS" if complete_ok else "FAIL"),
        ("numbers_untouched", "qty/value_usd still match Layer 3",
         "PASS" if numbers_untouched else "FAIL"),
    ]
    all_pass = _print_table(rows)
    print(f"\n  LAYER 5 VERIFICATION: {'PASS ✓' if all_pass else 'FAIL ✗'}")
    return all_pass


def main() -> int:
    case = run_decision()
    ok = verify(case)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
