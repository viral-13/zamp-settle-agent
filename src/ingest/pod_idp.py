"""Layer 1 — POD intelligent document processing. THE ONLY LLM STEP IN THIS LAYER.

This is deliberately the single module that calls an LLM. Its one job is to read
facts that are *explicitly written* on the Proof of Delivery and report them. It
must NEVER compute, invent, or alter a quantity or a dollar amount — those are
deterministic and live elsewhere. The arithmetic-free contract is enforced by the
downstream merge (build_evidence.py), which takes counts only from the EDI/GRN.

Flow:
  1. render_pod_pdf()  — deterministically rasterise the POD text to a real PDF.
  2. extract_pod_facts() — send that PDF (base64 document block) to Anthropic and
     get back STRICT JSON, parsed defensively with one retry.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from anthropic import Anthropic

# Swappable model constant (per the contract).
POD_MODEL = "claude-sonnet-4-6"

# Keys the POD extraction must return at the top level, with their expected types.
# bool is checked before int because bool is a subclass of int in Python.
_REQUIRED_TYPES = {
    "pallets_delivered": "int",
    "pod_shortage_noted": "bool",
    "pod_damage_noted": "bool",
    "pod_damage_detail": "str",
    "fields": "dict",
}

EXTRACTION_PROMPT = """\
You are an information-extraction step reading a carrier Proof of Delivery (POD).

Extract ONLY facts that are EXPLICITLY stated on the document. Do not infer,
calculate, or guess. If something is not explicitly written, mark it unknown
(use null). Critically: do NOT assume or report a shortage unless the document
explicitly records a shortage at delivery — "SHORTAGE AT DELIVERY: NONE" means
there is no shortage noted.

You report facts only. You must never compute or alter any quantity.

Return STRICT JSON and NOTHING ELSE — no prose, no markdown, no code fences.
Use exactly this shape:

{
  "pallets_delivered": <integer count of pallets delivered, or null if not stated>,
  "pod_shortage_noted": <true only if a shortage at delivery is explicitly recorded, else false>,
  "pod_damage_noted": <true only if damage is explicitly noted at delivery, else false>,
  "pod_damage_detail": "<verbatim or close paraphrase of the damage note; empty string if none>",
  "fields": {
    "pallets_delivered":  { "evidence_snippet": "<text you read it from>", "confidence": <0..1> },
    "pod_shortage_noted": { "evidence_snippet": "<text you read it from>", "confidence": <0..1> },
    "pod_damage_noted":   { "evidence_snippet": "<text you read it from>", "confidence": <0..1> },
    "pod_damage_detail":  { "evidence_snippet": "<text you read it from>", "confidence": <0..1> }
  }
}
"""


# --------------------------------------------------------------------------- #
# 1) deterministic PDF rendering (no LLM)
# --------------------------------------------------------------------------- #
def render_pod_pdf(txt_path: str | Path, pdf_path: str | Path) -> Path:
    """Render the POD text file into a real PDF using reportlab.

    Monospace font + line-by-line layout preserves the semi-structured look of the
    original receipt so the IDP step reads a realistic document.
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas

    text = Path(txt_path).read_text(encoding="utf-8")
    pdf_path = Path(pdf_path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(pdf_path), pagesize=LETTER)
    width, height = LETTER
    left = 0.6 * inch
    top = height - 0.75 * inch
    leading = 12.0
    c.setFont("Courier", 8.5)

    y = top
    for line in text.splitlines():
        c.drawString(left, y, line)
        y -= leading
        if y < 0.75 * inch:
            c.showPage()
            c.setFont("Courier", 8.5)
            y = top
    c.showPage()
    c.save()
    return pdf_path


# --------------------------------------------------------------------------- #
# 2) the LLM extraction
# --------------------------------------------------------------------------- #
def _strip_code_fences(s: str) -> str:
    """Remove a surrounding ```json ... ``` (or ``` ... ```) fence if present."""
    s = s.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.DOTALL)
    if m:
        return m.group(1).strip()
    return s


def _is_type(val, typ_name: str) -> bool:
    if typ_name == "bool":
        return isinstance(val, bool)
    if typ_name == "int":
        # Accept ints but never bools (bool is a subclass of int).
        return isinstance(val, int) and not isinstance(val, bool)
    if typ_name == "str":
        return isinstance(val, str)
    if typ_name == "dict":
        return isinstance(val, dict)
    return False


def _parse_strict(raw_text: str) -> dict:
    """Strip fences, json.loads, and validate the top-level shape/types."""
    data = json.loads(_strip_code_fences(raw_text))
    if not isinstance(data, dict):
        raise ValueError("POD JSON root is not an object")
    for key, typ_name in _REQUIRED_TYPES.items():
        if key not in data:
            raise ValueError(f"POD JSON missing required key '{key}'")
        if not _is_type(data[key], typ_name):
            raise ValueError(
                f"POD JSON key '{key}' must be {typ_name}, got {type(data[key]).__name__}"
            )
    return data


def extract_pod_facts(pdf_path: str | Path, client: Anthropic | None = None,
                      model: str = POD_MODEL) -> dict:
    """Send the POD PDF to Anthropic and return validated, strict-JSON facts.

    Parses defensively: strips fences, json.loads, validates types. On a parse or
    validation failure it retries the call ONCE, then raises.
    """
    client = client or Anthropic()
    pdf_b64 = base64.standard_b64encode(Path(pdf_path).read_bytes()).decode("ascii")

    def _call_model() -> str:
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    last_err: Exception | None = None
    for _attempt in range(2):  # initial try + one retry
        raw = _call_model()
        try:
            return _parse_strict(raw)
        except Exception as exc:  # parse/validation failure -> retry once
            last_err = exc
    raise RuntimeError(f"POD extraction failed to parse after retry: {last_err}")
