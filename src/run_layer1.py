"""Settle — Layer 1 (Ingestion) entrypoint.

`python src/run_layer1.py` does everything:
  1. parse the EDI 850 / 856 + GRN deterministically (and prove the ASN JSON is
     genuinely derived from the raw X12),
  2. render the POD to a real PDF and run the single LLM extraction step,
  3. compose the canonical case and write out/case_layer1.json,
  4. verify the Layer-1 evidence against data/ground_truth.json and print a table.

The POD facts come from the live LLM call, so a green table also proves the IDP
step works end to end.
"""

# --- load the API key from the repo-root .env BEFORE anything else needs it ---
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
# -----------------------------------------------------------------------------

import json
import sys

# Windows terminals default to cp1252; force UTF-8 so the table glyphs render.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ingest.parsers import (  # noqa: E402
    parse_asn_856,
    assert_asn_matches_json,
    parse_po_850,
    parse_grn,
)
from ingest.pod_idp import render_pod_pdf, extract_pod_facts, POD_MODEL  # noqa: E402
from ingest.build_evidence import build_case  # noqa: E402

DATA = ROOT / "data"
OUT = ROOT / "out"


# --------------------------------------------------------------------------- #
# ingestion pipeline
# --------------------------------------------------------------------------- #
def run_ingestion() -> dict:
    """Run the full Layer-1 ingestion and return the canonical case object."""
    OUT.mkdir(parents=True, exist_ok=True)

    # 1) deterministic parsers --------------------------------------------------
    print("[1/4] Deterministic parsing (EDI 850 / 856 + GRN)…")
    asn = parse_asn_856(DATA / "asn_856_raw.edi")
    assert_asn_matches_json(asn, DATA / "asn_856.json")
    print("      ASN parsed from raw X12 and matches asn_856.json ✓")
    po = parse_po_850(DATA / "po_850.json")
    grn = parse_grn(DATA / "grn.json")
    print(f"      PO {po['po_number']} • ordered={po['lines'][0]['ordered_qty']} | "
          f"ASN shipped={asn['lines'][0]['shipped_qty']}, pallets={asn['pallets_shipped']} | "
          f"GRN good/dmg/short={grn['lines'][0]['received_good']}/"
          f"{grn['lines'][0]['received_damaged']}/{grn['lines'][0]['received_short']}")

    # 2) POD -> PDF -> LLM extraction (the ONLY LLM step) -----------------------
    print(f"[2/4] Rendering POD to PDF and extracting facts via LLM ({POD_MODEL})…")
    pdf_path = render_pod_pdf(DATA / "pod_delivery_receipt.txt", OUT / "pod.pdf")
    print(f"      Wrote {pdf_path.relative_to(ROOT)}")
    pod_facts = extract_pod_facts(pdf_path)
    print(f"      LLM extracted: pallets_delivered={pod_facts['pallets_delivered']}, "
          f"pod_shortage_noted={pod_facts['pod_shortage_noted']}, "
          f"pod_damage_noted={pod_facts['pod_damage_noted']}")

    # 3) compose canonical case -------------------------------------------------
    print("[3/4] Composing canonical case (evidence block)…")
    case = build_case(po, asn, grn, pod_facts)
    out_path = OUT / "case_layer1.json"
    out_path.write_text(json.dumps(case, indent=2), encoding="utf-8")
    print(f"      Wrote {out_path.relative_to(ROOT)}")

    return case


# --------------------------------------------------------------------------- #
# verification against the dev/eval ground truth
# --------------------------------------------------------------------------- #
def verify(case: dict) -> bool:
    """Assert Layer-1 evidence matches ground_truth.expected_evidence; print a table."""
    print("[4/4] Verifying against data/ground_truth.json…\n")
    gt = json.loads((DATA / "ground_truth.json").read_text(encoding="utf-8"))
    exp = gt["expected_evidence"]
    ev = case["evidence"]
    po_line = case["po_line"]

    # (label, actual, expected, kind). kind="exact" for ints/bools, "nonempty" for free text.
    checks = [
        ("ordered_qty",         po_line["ordered_qty"],      exp["ordered_qty"],              "exact"),
        ("shipped_qty",         ev["shipped_qty"],           exp["shipped_qty_per_asn"],      "exact"),
        ("pallets_delivered",   ev["pallets_delivered"],     exp["pallets_delivered_per_pod"],"exact"),
        ("pod_shortage_noted",  ev["pod_shortage_noted"],    exp["pod_shortage_noted"],       "exact"),
        ("pod_damage_noted",    ev["pod_damage_noted"],      exp["pod_damage_noted"],         "exact"),
        ("received_good",       ev["received_good"],         exp["received_good"],            "exact"),
        ("received_damaged",    ev["received_damaged"],      exp["received_damaged"],         "exact"),
        ("received_short",      ev["received_short"],        exp["received_short"],           "exact"),
        ("pod_damage_detail",   ev["pod_damage_detail"],     "<non-empty>",                   "nonempty"),
    ]

    name_w = max(len(c[0]) for c in checks)
    header = f"  {'FIELD'.ljust(name_w)}   {'ACTUAL'.ljust(22)} {'EXPECTED'.ljust(14)} RESULT"
    print(header)
    print("  " + "-" * (len(header) - 2))

    all_pass = True
    for label, actual, expected, kind in checks:
        if kind == "nonempty":
            # Exact type/value for booleans+ints; for free text only require non-empty.
            ok = isinstance(actual, str) and actual.strip() != ""
            actual_disp = (actual[:19] + "…") if isinstance(actual, str) and len(actual) > 20 else actual
        else:
            ok = (type(actual) is type(expected)) and (actual == expected)
            actual_disp = actual
        all_pass = all_pass and ok
        status = "PASS" if ok else "FAIL"
        print(f"  {label.ljust(name_w)}   {str(actual_disp).ljust(22)} "
              f"{str(expected).ljust(14)} {status}")

    print()
    print(f"  LAYER 1 VERIFICATION: {'PASS ✓' if all_pass else 'FAIL ✗'}")
    return all_pass


def main() -> int:
    case = run_ingestion()
    print()
    ok = verify(case)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
