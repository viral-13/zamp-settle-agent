"""Layer 5 — policy gate, action selection, payable, finance-handoff stub. DETERMINISTIC.

The routing gate (auto vs escalate) and every dollar figure here are plain Python,
using thresholds from config. The LLM has no say in routing and produces no number.
The finance handoff is a STUB — a structured object, not a real integration.
"""

from __future__ import annotations


def _money(qty: int, unit_price: float) -> float:
    """Dollar value of a quantity (whole amounts presented as ints)."""
    value = qty * unit_price
    return int(value) if float(value).is_integer() else value


# --------------------------------------------------------------------------- #
# routing gate + action selection
# --------------------------------------------------------------------------- #
def route(confidence: float, value_usd: float, config: dict) -> str:
    """Deterministic gate: auto only if confident enough AND within the value limit."""
    threshold = config["confidence_routing"]["auto_act_threshold"]
    max_value = config["policy_limits"]["auto_claim_max_value_usd"]
    if confidence >= threshold and value_usd <= max_value:
        return "auto"
    return "escalate"


def select_action(cause: str, config: dict) -> str:
    """Pick the action for a cause from config.liability_rules (not hardcoded)."""
    rules = config["liability_rules"]
    if cause not in rules or "action" not in rules[cause]:
        raise ValueError(f"no action defined in config.liability_rules for cause {cause!r}")
    return rules[cause]["action"]


# --------------------------------------------------------------------------- #
# payable (deterministic)
# --------------------------------------------------------------------------- #
def compute_payable(case: dict) -> dict:
    """Deterministic payable position. Every figure = qty * unit_price from the record."""
    po_line = case["po_line"]
    ev = case["evidence"]
    unit_price = po_line["unit_price"]

    ordered = po_line["ordered_qty"]
    good = ev["received_good"]
    damaged = ev["received_damaged"]
    short = ev["received_short"]

    return {
        "invoice_expected_usd": _money(ordered, unit_price),       # 500 * 40 = 20000
        "payable_good_usd": _money(good, unit_price),              # 460 * 40 = 18400
        "leakage_caught_usd": _money(damaged + short, unit_price), # (25+15) * 40 = 1600
    }


# --------------------------------------------------------------------------- #
# finance handoff STUB (asserted, not built)
# --------------------------------------------------------------------------- #
def build_handoff_stub(case: dict, payable: dict, claim_refs: list[str]) -> dict:
    """Emit a structured finance-handoff object. Clearly labeled STUB.

    Demonstrates: when the supplier's 810 invoice arrives, finance already holds the
    settled $18,400 position, so it does NOT open a fresh AP exception.
    """
    po_line = case["po_line"]
    return {
        "payable_good_usd": payable["payable_good_usd"],
        "claims": claim_refs,
        "incoming_invoice_810_stub": {
            "qty": po_line["ordered_qty"],                 # 500
            "value": payable["invoice_expected_usd"],      # 20000
        },
        "status": "pre-cleared",
        "note": "STUB — asserted, not a real integration",
    }
