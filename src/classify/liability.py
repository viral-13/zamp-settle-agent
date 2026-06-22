"""Layer 4 — liability classification. THE JUDGMENT STEP (LLM reasons; never computes).

For each discrepancy the model is given (a) the discrepancy type+qty, (b) the
assembled evidence block, and (c) the GENERAL liability policy from config — described
as policy, NOT as the answer for this case. The model must reach the liable party
itself. It must not invent facts, must not change any number, and must calibrate
confidence by corroboration (HIGH when multiple independent documents agree; LOW when
inferred from absence or a single source).

Quantities/dollar values are fixed by Layer 3 and pass through untouched — this module
only writes a `classification` block. ground_truth.json is never in this context.
"""

from __future__ import annotations

import json
import re

from anthropic import Anthropic

# Swappable model constant.
CLASSIFIER_MODEL = "claude-sonnet-4-6"

_VALID_CAUSE = {"in_transit_damage", "carrier_loss", "supplier_short_load", "data_error", "unknown"}
_VALID_LIABLE = {"carrier", "supplier", "none", "unknown"}

SYSTEM_INSTRUCTIONS = """\
You are a freight/OS&D liability analyst. You are given ONE discrepancy on a received
shipment, the assembled evidence for that shipment, and a GENERAL liability policy.

Your job: reason ONLY from the provided evidence and the general policy, and decide the
most likely cause and the liable party YOURSELF. Rules you must follow:
  - Do NOT invent facts that are not in the evidence.
  - Do NOT change, recompute, or restate any quantity or dollar amount. You classify only.
  - Calibrate confidence by CORROBORATION: assign HIGH confidence (>= 0.8) only when a
    fact is independently corroborated by multiple documents (e.g. POD AND GRN agree).
    Assign LOW confidence (< 0.8) when the cause is inferred from the ABSENCE of a note
    or rests on a single source.
  - If the evidence is insufficient to decide, return cause "unknown", liable_party
    "unknown", with low confidence.
  - Cite the SPECIFIC evidence facts you used (e.g. "POD: pod_damage_noted=true",
    "GRN: received_damaged=25", "POD: pod_shortage_noted=false").

Return STRICT JSON and NOTHING ELSE — no prose, no markdown, no code fences:
{
  "cause": one of ["in_transit_damage","carrier_loss","supplier_short_load","data_error","unknown"],
  "liable_party": one of ["carrier","supplier","none","unknown"],
  "confidence": <number 0.0-1.0>,
  "rationale": "<1-2 sentences of reasoning>",
  "evidence_cited": ["<specific evidence fact>", "..."]
}
"""


def _policy_text(config: dict) -> str:
    """The general liability policy, described as policy (not the answer for this case)."""
    return json.dumps(config["liability_rules"], indent=2)


def _build_user_prompt(discrepancy: dict, evidence: dict, config: dict) -> str:
    disc = {"type": discrepancy["type"], "qty": discrepancy["qty"]}
    return (
        "GENERAL LIABILITY POLICY (applies to any shipment; not the answer for this case):\n"
        f"{_policy_text(config)}\n\n"
        "UNIT OF MEASURE: quantities are in cases. cases_per_pallet = "
        f"{config['supplier_mapping']['uom']['cases_per_pallet']}.\n\n"
        "ASSEMBLED EVIDENCE for this shipment:\n"
        f"{json.dumps(evidence, indent=2)}\n\n"
        "THE DISCREPANCY to classify (do not change its numbers):\n"
        f"{json.dumps(disc, indent=2)}\n\n"
        "Classify the cause and liable party per the policy and evidence. Return strict JSON only."
    )


def _strip_code_fences(s: str) -> str:
    s = s.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.DOTALL)
    return m.group(1).strip() if m else s


def _parse_strict(raw_text: str) -> dict:
    data = json.loads(_strip_code_fences(raw_text))
    if not isinstance(data, dict):
        raise ValueError("classification JSON root is not an object")

    cause = data.get("cause")
    if cause not in _VALID_CAUSE:
        raise ValueError(f"invalid cause: {cause!r}")
    liable = data.get("liable_party")
    if liable not in _VALID_LIABLE:
        raise ValueError(f"invalid liable_party: {liable!r}")

    conf = data.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        raise ValueError(f"confidence must be a number, got {type(conf).__name__}")
    if not (0.0 <= float(conf) <= 1.0):
        raise ValueError(f"confidence out of range: {conf}")

    rationale = data.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("rationale must be a non-empty string")

    cited = data.get("evidence_cited")
    if not isinstance(cited, list) or not cited or not all(isinstance(c, str) and c.strip() for c in cited):
        raise ValueError("evidence_cited must be a non-empty list of strings")

    return {
        "cause": cause,
        "liable_party": liable,
        "confidence": float(conf),
        "rationale": rationale.strip(),
        "evidence_cited": [c.strip() for c in cited],
    }


def classify_discrepancy(discrepancy: dict, evidence: dict, config: dict,
                         client: Anthropic | None = None,
                         model: str = CLASSIFIER_MODEL) -> dict:
    """One Sonnet call per discrepancy. Returns a validated classification block.

    Parses defensively (strip fences, json.loads, validate enums/types). Retries once
    on parse/validation failure, then raises.
    """
    client = client or Anthropic()
    user_prompt = _build_user_prompt(discrepancy, evidence, config)

    def _call_model() -> str:
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            system=SYSTEM_INSTRUCTIONS,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    last_err: Exception | None = None
    for _attempt in range(2):  # initial try + one retry
        raw = _call_model()
        try:
            return _parse_strict(raw)
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"liability classification failed to parse after retry: {last_err}")
