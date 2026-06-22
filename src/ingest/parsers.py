"""Layer 1 — DETERMINISTIC document parsers.

This module contains NO LLM calls and never sees the POD. Every value here is
derived by plain Python from the structured source documents (raw X12 EDI and the
JSON exports). Keeping this separate from `pod_idp.py` makes the split explicit:
arithmetic and structured parsing live here; intelligent extraction lives there.
"""

from __future__ import annotations

import json
from pathlib import Path


# --------------------------------------------------------------------------- #
# small IO helpers
# --------------------------------------------------------------------------- #
def _read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def _read_json(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _strip_meta(d: dict) -> dict:
    """Drop documentation-only keys (anything starting with '_') for comparison."""
    return {k: v for k, v in d.items() if not k.startswith("_")}


# --------------------------------------------------------------------------- #
# EDI 856 (ASN) — parsed from the RAW X12, segment by segment
# --------------------------------------------------------------------------- #
def parse_asn_856(path: str | Path) -> dict:
    """Parse a raw X12 856 Advance Ship Notice into a structured dict.

    Segment map used (per the contract):
        ISA / GS  -> supplier_id (interchange sender)
        BSN       -> asn_number, ship_date
        TD1       -> pallets_shipped
        TD5       -> carrier_scac
        REF*BM    -> bol_number
        PRF       -> po_number
        LIN VP    -> supplier_item_code
        SN1       -> shipped_qty, uom

    Pure string parsing — no LLM, no inference.
    """
    raw = _read_text(path)

    # X12: segments end with '~', elements are '*'-delimited. Newlines are cosmetic.
    segments = [seg.strip() for seg in raw.replace("\n", "").split("~") if seg.strip()]

    asn: dict = {
        "doc_type": "EDI_856_ASN",
        "asn_number": None,
        "po_number": None,
        "supplier_id": None,
        "ship_date": None,
        "carrier_scac": None,
        "bol_number": None,
        "pallets_shipped": None,
        "lines": [],
    }
    current_line: dict | None = None

    def _fmt_date(yyyymmdd: str) -> str:
        return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"

    for seg in segments:
        el = seg.split("*")
        tag = el[0]

        if tag == "ISA":
            # ISA06 = interchange sender ID (the supplier), space-padded.
            asn["supplier_id"] = el[6].strip()
        elif tag == "GS" and not asn["supplier_id"]:
            # GS02 also carries the application sender ID, as a fallback.
            asn["supplier_id"] = el[2].strip()
        elif tag == "BSN":
            # BSN02 = shipment identification, BSN03 = date (YYYYMMDD).
            asn["asn_number"] = el[2].strip()
            asn["ship_date"] = _fmt_date(el[3].strip())
        elif tag == "TD1":
            # TD1*PLT71*20****G*12000*LB  -> element 2 is the lading/pallet quantity.
            asn["pallets_shipped"] = int(el[2])
        elif tag == "TD5":
            # TD5**2*CARRIERX*M  -> element 3 is the carrier code (SCAC).
            asn["carrier_scac"] = el[3].strip()
        elif tag == "REF":
            # REF*BM*BOL778899 -> BM qualifier = Bill of Lading number.
            if len(el) > 2 and el[1] == "BM":
                asn["bol_number"] = el[2].strip()
        elif tag == "PRF":
            # PRF*PO500123 -> purchase order number.
            asn["po_number"] = el[1].strip()
        elif tag == "LIN":
            # LIN**VP*SUP-4471 -> VP qualifier = Vendor's (supplier) item number.
            current_line = {
                "line_no": len(asn["lines"]) + 1,
                "supplier_item_code": None,
                "shipped_qty": None,
                "uom": None,
            }
            for i in range(1, len(el) - 1):
                if el[i] == "VP":
                    current_line["supplier_item_code"] = el[i + 1].strip()
            asn["lines"].append(current_line)
        elif tag == "SN1":
            # SN1**500*CA -> element 2 = shipped qty, element 3 = unit of measure.
            if current_line is None:
                current_line = {
                    "line_no": len(asn["lines"]) + 1,
                    "supplier_item_code": None,
                    "shipped_qty": None,
                    "uom": None,
                }
                asn["lines"].append(current_line)
            current_line["shipped_qty"] = int(el[2])
            uom = el[3].strip() if len(el) > 3 else ""
            current_line["uom"] = "case" if uom == "CA" else uom

    return asn


def assert_asn_matches_json(parsed: dict, json_path: str | Path) -> None:
    """Prove the structured ASN is genuinely derived from the EDI, not hand-fed.

    Compares the EDI-parsed dict to the committed `asn_856.json` (ignoring the
    documentation-only `_meta` key). Raises AssertionError on any mismatch.
    """
    expected = _strip_meta(_read_json(json_path))
    if parsed != expected:
        # Build a readable field-level diff.
        diffs = []
        for key in sorted(set(parsed) | set(expected)):
            if parsed.get(key) != expected.get(key):
                diffs.append(f"  {key}: parsed={parsed.get(key)!r} != json={expected.get(key)!r}")
        raise AssertionError(
            "ASN parsed from EDI does not match asn_856.json:\n" + "\n".join(diffs)
        )


# --------------------------------------------------------------------------- #
# EDI 850 (PO) and GRN — load structured JSON exports, validate shape/types
# --------------------------------------------------------------------------- #
def _require(obj: dict, key: str, typ, where: str):
    if key not in obj:
        raise ValueError(f"{where}: missing required field '{key}'")
    val = obj[key]
    # bool is a subclass of int — guard so a bool can't masquerade as an int.
    if typ is int and isinstance(val, bool):
        raise ValueError(f"{where}: field '{key}' must be {typ.__name__}, got bool")
    if not isinstance(val, typ):
        raise ValueError(
            f"{where}: field '{key}' must be {typ.__name__}, got {type(val).__name__}"
        )
    return val


def parse_po_850(path: str | Path) -> dict:
    """Load the parsed EDI 850 PO export and validate required fields/types."""
    po = _read_json(path)
    _require(po, "po_number", str, "PO 850")
    _require(po, "supplier_id", str, "PO 850")
    _require(po, "currency", str, "PO 850")
    _require(po, "lines", list, "PO 850")
    if not po["lines"]:
        raise ValueError("PO 850: 'lines' is empty")
    for i, line in enumerate(po["lines"]):
        where = f"PO 850 line[{i}]"
        _require(line, "line_no", int, where)
        _require(line, "buyer_sku", str, where)
        _require(line, "ordered_qty", int, where)
        _require(line, "unit_price", (int, float), where)
        _require(line, "uom", str, where)
    return po


def parse_grn(path: str | Path) -> dict:
    """Load the GRN (WMS goods-receipt export) and validate required fields/types."""
    grn = _read_json(path)
    _require(grn, "grn_number", str, "GRN")
    _require(grn, "po_number", str, "GRN")
    _require(grn, "lines", list, "GRN")
    if not grn["lines"]:
        raise ValueError("GRN: 'lines' is empty")
    for i, line in enumerate(grn["lines"]):
        where = f"GRN line[{i}]"
        _require(line, "line_no", int, where)
        _require(line, "buyer_sku", str, where)
        _require(line, "received_good", int, where)
        _require(line, "received_damaged", int, where)
        _require(line, "received_short", int, where)
    return grn
