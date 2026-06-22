"""Layer 3 — reconciliation engine. 100% DETERMINISTIC. No LLM. No cause/liability.

Records the FACT of each discrepancy (type, qty, value) and proves the books close
exactly, asserted two independent ways. Cause and liability are Layer 4; this module
must never say "carrier" or "supplier". Every dollar value is qty * unit_price, with
unit_price taken from po_line — nothing is hardcoded.
"""

from __future__ import annotations


def _money(qty: int, unit_price: float) -> float:
    """Dollar value of a quantity. The ONLY place value_usd is computed here."""
    value = qty * unit_price
    # Present whole-dollar amounts as ints (40.0 * 25 -> 1000, not 1000.0).
    return int(value) if float(value).is_integer() else value


def build_discrepancies(evidence: dict, po_line: dict) -> list[dict]:
    """Build the discrepancies[] array. One record per OS&D fact. No classification.

    `classification` and `decision` are left as empty objects for Layers 4 & 5.
    """
    po_number = po_line["po_number"]
    unit_price = po_line["unit_price"]
    ordered = po_line["ordered_qty"]
    good = evidence["received_good"]
    damaged = evidence["received_damaged"]
    short = evidence["received_short"]

    discrepancies = [
        {
            "discrepancy_id": f"DISC-{po_number}-DAMAGED",
            "type": "damaged",
            "qty": damaged,
            "value_usd": _money(damaged, unit_price),
            "derived_from": ["evidence.received_damaged", "evidence.pod_damage_noted"],
            "classification": {},  # Layer 4
            "decision": {},        # Layer 5
        },
        {
            "discrepancy_id": f"DISC-{po_number}-SHORT",
            "type": "short",
            "qty": short,
            "value_usd": _money(short, unit_price),
            "derived_from": [
                "evidence.received_short",
                "evidence.pod_shortage_noted",
                "computed: ordered - good - damaged",
            ],
            "classification": {},  # Layer 4
            "decision": {},        # Layer 5
        },
    ]
    return discrepancies


def reconcile(evidence: dict, po_line: dict) -> dict:
    """Prove the books close two independent ways; return the reconciliation block.

    1) good + damaged + short == ordered
    2) independently compute short = ordered - good - damaged and assert it equals
       the GRN-reported received_short.

    Raises a data-integrity error (loudly) if either check fails.
    """
    ordered = po_line["ordered_qty"]
    good = evidence["received_good"]
    damaged = evidence["received_damaged"]
    short = evidence["received_short"]

    accounted_total = good + damaged + short
    independent_short = ordered - good - damaged

    checks = [
        {
            "check": "good + damaged + short == ordered",
            "expected": ordered,
            "actual": accounted_total,
            "result": "PASS" if accounted_total == ordered else "FAIL",
        },
        {
            "check": "independent short (ordered - good - damaged) == GRN received_short",
            "expected": short,
            "actual": independent_short,
            "result": "PASS" if independent_short == short else "FAIL",
        },
    ]

    closes = all(c["result"] == "PASS" for c in checks)
    if not closes:
        failed = [c for c in checks if c["result"] == "FAIL"]
        raise ValueError(
            "Reconciliation data-integrity error — books do not close: "
            + "; ".join(f"{c['check']} (expected {c['expected']}, got {c['actual']})" for c in failed)
        )

    return {
        "ordered": ordered,
        "good": good,
        "damaged": damaged,
        "short": short,
        "accounted_total": accounted_total,
        "closes": closes,
        "checks": checks,
    }
