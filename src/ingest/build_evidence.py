"""Layer 1 — compose the canonical `evidence` block. DETERMINISTIC merge.

Takes the deterministic facts (PO / ASN / GRN) and the LLM-extracted POD facts and
assembles them into the `evidence` block of the canonical case object. This module
does NO arithmetic: every count is copied straight from its source document. In
particular `received_short` is taken verbatim from the GRN — it is NOT recomputed
against the LLM output (the LLM never touches a quantity).

Layers 2-5 fill discrepancies / decision / payable; those are left empty here.
"""

from __future__ import annotations


def build_evidence(po: dict, asn: dict, grn: dict, pod_facts: dict) -> dict:
    """Merge the four sources into the canonical evidence block.

    Field provenance:
        shipped_qty, pallets_shipped              <- ASN 856 (deterministic)
        pallets_delivered, pod_shortage_noted,
        pod_damage_noted, pod_damage_detail       <- POD (LLM-extracted)
        received_good, received_damaged,
        received_short                            <- GRN (deterministic, verbatim)
    """
    asn_line = asn["lines"][0]
    grn_line = grn["lines"][0]

    return {
        # --- from ASN 856 (deterministic) ---
        "shipped_qty": asn_line["shipped_qty"],
        "pallets_shipped": asn["pallets_shipped"],
        # --- from POD (LLM-extracted facts only) ---
        "pallets_delivered": pod_facts["pallets_delivered"],
        "pod_shortage_noted": pod_facts["pod_shortage_noted"],
        "pod_damage_noted": pod_facts["pod_damage_noted"],
        "pod_damage_detail": pod_facts["pod_damage_detail"],
        # --- from GRN (deterministic; received_short taken as-is, NOT recomputed) ---
        "received_good": grn_line["received_good"],
        "received_damaged": grn_line["received_damaged"],
        "received_short": grn_line["received_short"],
    }


def build_case(po: dict, asn: dict, grn: dict, pod_facts: dict,
               case_id: str = "CASE-PO500123-L1") -> dict:
    """Assemble the canonical case object with po_line + evidence populated.

    discrepancies / payable are intentionally left empty for later layers.
    """
    po_line = po["lines"][0]

    return {
        "case_id": case_id,
        "status": "open",
        "po_line": {
            "po_number": po["po_number"],
            "sku": po_line["buyer_sku"],
            "ordered_qty": po_line["ordered_qty"],
            "unit_price": po_line["unit_price"],
            "currency": po["currency"],
        },
        "evidence": build_evidence(po, asn, grn, pod_facts),
        # --- left for later layers ---
        "discrepancies": [],   # Layer 3 reconciliation + Layer 4 classification
        "payable": {},         # Layer 5 decision
    }
