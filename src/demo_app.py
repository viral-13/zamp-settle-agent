"""Settle — Layer 6 demo surface (Receiving Dock Operations Console).

A thin FastAPI shell whose only job is to make the engine's judgment legible on
camera. It NEVER reimplements engine logic or hardcodes a figure:

  GET  /            -> the single-page console (static/index.html)
  GET  /case        -> the last settled case + packets + audit (read from disk)
  POST /run         -> EXECUTES the real pipeline by calling each layer's main()
                       (Layers 1-5), captures their PASS/FAIL tables, returns the
                       fresh settled case + packets + audit + verification.
  GET  /doc/{name}  -> raw content of a source document (po/asn/pod/grn)

Every number shown in the UI comes from this engine output; the front-end fabricates
nothing.
"""

from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import io
import json
import re
import sys
from contextlib import redirect_stdout

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

OUT = ROOT / "out"
DATA = ROOT / "data"
CONFIG = ROOT / "config" / "config.json"
STATIC = SRC / "static"

# Import the real layer entrypoints — calling these is how /run executes the pipeline.
import run_layer1  # noqa: E402
import run_layer2  # noqa: E402
import run_layer3  # noqa: E402
import run_layer4  # noqa: E402
import run_layer5  # noqa: E402

# The 5 executable layers, in order. (Handoff is produced inside Decide/Layer 5 and
# is surfaced as the 6th rail stage in the UI.)
PIPELINE = [
    ("Ingest", run_layer1),
    ("Resolve", run_layer2),
    ("Reconcile", run_layer3),
    ("Classify", run_layer4),
    ("Decide", run_layer5),
]

DOC_MAP = {
    "po":  ("PO 850 — Purchase Order", DATA / "po_850.json"),
    "asn": ("ASN 856 — Advance Ship Notice (raw X12)", DATA / "asn_856_raw.edi"),
    "pod": ("POD — Proof of Delivery (annotated)", DATA / "pod_delivery_receipt.txt"),
    "grn": ("GRN — Goods Receipt Note", DATA / "grn.json"),
}

app = FastAPI(title="Settle — Receiving Dock Operations Console")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def _parse_table(text: str) -> list[dict]:
    """Pull {check, result} rows out of a captured PASS/FAIL table."""
    rows = []
    for line in text.splitlines():
        m = re.match(r"\s*(\S+)\s{2,}.*\b(PASS|FAIL)\s*$", line)
        if m:
            rows.append({"check": m.group(1), "result": m.group(2)})
    return rows


def _load_claims(case: dict) -> list[dict]:
    claims = []
    for d in case.get("discrepancies", []):
        ref = (d.get("decision") or {}).get("claim_packet_ref")
        if not ref:
            continue
        p = OUT / "claims" / f"{ref}.json"
        if p.exists():
            claims.append(json.loads(p.read_text(encoding="utf-8")))
    return claims


def _build_payload(verification=None) -> dict:
    case_path = OUT / "case_layer5.json"
    if not case_path.exists():
        raise HTTPException(404, "No settled case yet. Press Run Settle to execute the pipeline.")
    case = json.loads(case_path.read_text(encoding="utf-8"))
    claims = _load_claims(case)
    cfg = _load_config()

    supplier_id = carrier_scac = None
    for pk in claims:
        cp = pk.get("counterparty", {})
        if cp.get("role") == "supplier":
            supplier_id = cp.get("id")
        elif cp.get("role") == "carrier":
            carrier_scac = cp.get("id")

    context = {
        "product": "Settle",
        "tagline": "Receiving Dock Operations Console",
        "po_number": case["po_line"]["po_number"],
        "sku": case["po_line"]["sku"],
        "supplier_id": supplier_id,
        "carrier_scac": carrier_scac,
        "invoice_expected_usd": (case.get("payable") or {}).get("invoice_expected_usd"),
    }

    return {
        "case": case,
        "claims": claims,
        "audit": case.get("audit", []),
        "context": context,
        "threshold": cfg["confidence_routing"]["auto_act_threshold"],
        "policy_limit_usd": cfg["policy_limits"]["auto_claim_max_value_usd"],
        "verification": verification,
    }


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/case")
def get_case() -> dict:
    """The last settled case, read straight from engine output (no re-run).

    Returns a clear empty signal (`available: false`) when nothing is saved yet, so
    the front-end can disable Replay without treating it as an error.
    """
    if not (OUT / "case_layer5.json").exists():
        return {"available": False}
    payload = _build_payload()
    payload["available"] = True
    return payload


@app.post("/run")
def run() -> dict:
    """Execute the full pipeline by calling each layer's real main(); capture results."""
    verification = []
    for stage, mod in PIPELINE:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mod.main()
        text = buf.getvalue()
        verification.append({
            "stage": stage,
            "passed": code == 0,
            "rows": _parse_table(text),
            "raw": text,
        })

    # Handoff stage — produced inside Decide (Layer 5); reflect its status from the case.
    case = json.loads((OUT / "case_layer5.json").read_text(encoding="utf-8"))
    handoff = case.get("finance_handoff", {})
    handoff_ok = bool(handoff) and handoff.get("status") == "pre-cleared"
    verification.append({
        "stage": "Handoff",
        "passed": handoff_ok,
        "rows": [{"check": "finance_handoff_stub", "result": "PASS" if handoff_ok else "FAIL"}],
        "raw": handoff.get("note", ""),
    })

    return _build_payload(verification=verification)


@app.get("/doc/{name}", response_class=PlainTextResponse)
def doc(name: str) -> str:
    if name not in DOC_MAP:
        raise HTTPException(404, f"Unknown document '{name}'. Try one of: po, asn, pod, grn.")
    _title, path = DOC_MAP[name]
    return path.read_text(encoding="utf-8")
