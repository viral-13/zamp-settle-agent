"""Layer 2 — entity resolution + UoM normalization. DETERMINISTIC by default.

Identity resolution rule (non-negotiable):
  * config.supplier_mapping.item_code_map is the SOURCE OF TRUTH.
  * A mapping is auto-applied ONLY when the code is confirmed in that table
    (method="table", routing="auto").
  * An unknown/unconfirmed code NEVER auto-applies. It routes to "escalate". The
    LLM may *suggest* a candidate (Haiku) but that suggestion is advisory only —
    resolved_sku stays null until a human confirms it into the table.

All UoM math is plain Python. No quantity or dollar value originates from the LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

# Swappable constant — used ONLY for the optional unknown-code suggestion, which
# the locked scenario never exercises.
SUGGEST_MODEL = "claude-haiku-4-5-20251001"


def load_config(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# 1) entity resolution
# --------------------------------------------------------------------------- #
def resolve_item(supplier_code: str, config: dict, *, suggest: bool = False,
                 client=None) -> dict:
    """Resolve a supplier item code to a buyer SKU using the maintained table.

    Confirmed in the table  -> auto-applied (method="table", routing="auto").
    Not in the table         -> escalate; resolved_sku is null; an optional Haiku
                                suggestion may be attached but is never applied.

    `suggest=False` by default so the main run (and the unknown-code unit test)
    make ZERO model calls.
    """
    item_code_map = config["supplier_mapping"]["item_code_map"]

    if supplier_code in item_code_map:
        return {
            "supplier_code": supplier_code,
            "resolved_sku": item_code_map[supplier_code],
            "method": "table",
            "confidence": 1.0,
            "routing": "auto",
            "source": "supplier_mapping",
        }

    # Unknown / unconfirmed code — must escalate, never auto-apply.
    suggested = None
    if suggest:
        suggested = suggest_candidate(supplier_code, config, client=client)

    return {
        "supplier_code": supplier_code,
        "resolved_sku": None,
        "method": "unconfirmed",
        "confidence": 0.0,
        "routing": "escalate",
        "source": None,
        "suggested_candidate": suggested,  # advisory only; requires human confirmation
    }


def suggest_candidate(supplier_code: str, config: dict, *, client=None) -> str | None:
    """OPTIONAL: ask Haiku for a candidate SKU for an UNKNOWN code (advisory only).

    Returns a SKU string or None. Never used by the main case. The suggestion is
    never auto-applied — resolve_item keeps resolved_sku null regardless.
    """
    known_skus = sorted(set(config["supplier_mapping"]["item_code_map"].values()))
    if client is None:
        from anthropic import Anthropic
        client = Anthropic()

    prompt = (
        "A supplier item code is not in our confirmed mapping table. Suggest the "
        "single most likely buyer SKU from the known list, or reply NONE if unsure. "
        "Reply with ONLY the SKU token (or NONE) — no other text.\n"
        f"Unknown supplier code: {supplier_code}\n"
        f"Known buyer SKUs: {', '.join(known_skus)}"
    )
    resp = client.messages.create(
        model=SUGGEST_MODEL,
        max_tokens=16,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    if not text or text.upper() == "NONE":
        return None
    return text


# --------------------------------------------------------------------------- #
# 2) UoM normalization & cross-check (deterministic)
# --------------------------------------------------------------------------- #
def uom_checks(evidence: dict, config: dict) -> list[dict]:
    """Deterministic UoM cross-checks. All quantities stay in buyer UoM (cases).

    Records each check as {check, expected, actual, result}.
    """
    cpp = config["supplier_mapping"]["uom"]["cases_per_pallet"]
    pallets_shipped = evidence["pallets_shipped"]
    pallets_delivered = evidence["pallets_delivered"]
    shipped_qty = evidence["shipped_qty"]

    checks = []

    # pallets_shipped * cases_per_pallet == shipped_qty  (20 * 25 == 500)
    actual = pallets_shipped * cpp
    checks.append({
        "check": f"pallets_shipped({pallets_shipped}) * cases_per_pallet({cpp}) == shipped_qty",
        "expected": shipped_qty,
        "actual": actual,
        "result": "PASS" if actual == shipped_qty else "FAIL",
    })

    # delivered pallet count consistent with shipped pallet count (20 == 20)
    checks.append({
        "check": "pallets_delivered == pallets_shipped",
        "expected": pallets_shipped,
        "actual": pallets_delivered,
        "result": "PASS" if pallets_delivered == pallets_shipped else "FAIL",
    })

    return checks
