# Settle — Operations Digital Employee (Zamp MVP)
### Order-to-Receipt OS&D / Claims Resolution Agent

A working MVP for the Zamp AI PM application. On a single inbound shipment, **Settle** reconciles what was ordered vs. shipped vs. received, decides **who is liable** for each discrepancy (carrier vs. supplier), drafts the claim where it's confident, escalates where it isn't, and hands finance a clean payable position — all with its reasoning and confidence on screen.

See `Zamp_OSD_Agent_PRD.md` for the full strategy and pitch. This repo is the build.

---

## Build target
Real, runnable Python project. The LLM steps (POD extraction + liability classification) call the Anthropic / Gemini API at runtime with **your own key**. Layer 6 adds a thin local web UI to make the judgment legible for screen-recording the 2-min video.

## The locked scenario (synthetic)
PO: **500 cases of SKU-A @ $40 = $20,000.** At receiving: **460 good, 25 damaged, 15 short.**
- 25 damaged → POD-annotated + GRN-inspected → **carrier claim, $1,000**, high confidence → auto-draft.
- 15 short → full pallets delivered, none noted short → inferred **supplier short-load, $600**, low confidence → escalate.
- Payable-good = **$18,400**. Leakage caught & assigned = **$1,600** before finance ever sees the invoice.

## Build layers (each independently testable)
- **Layer 0 — Data & schema** ✅ *(this delivery)* — synthetic documents, canonical schema, config.
- **Layer 1 — Ingestion** — deterministic parsers for EDI 850/856 + GRN; LLM-IDP for the POD PDF.
- **Layer 2 — Normalize & resolve** — canonical model; cases↔pallets / item-code resolution.
- **Layer 3 — Reconciliation engine** — deterministic deltas (460/25/15) + tolerance flags.
- **Layer 4 — Classification (judgment)** — LLM assigns cause + liable party + confidence + evidence. *Hero step.*
- **Layer 5 — Decision & action** — policy-gated: auto-draft carrier claim; draft + escalate supplier debit; set payable.
- **Layer 6 — Audit + demo UI** — audit log; surface reasoning, confidence, claim packets, recovered-$, finance-handoff stub.

## Non-negotiables (from the PRD)
- **LLM vs. deterministic split:** the LLM does extraction, liability classification, action selection, and rationale. **Deterministic code does all arithmetic, tolerances, matching, actions, and the audit log.** The LLM never computes the numbers.
- **BUILT vs. ASSERTED:** real EDI/carrier/ERP integrations, async arrival, multi-partner mapping, and the live finance handoff are **asserted in the pitch, not built**. The finance handoff is shown as a stub.

## Run (will fill in as layers land)
```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# Layer 1+ entrypoints added as we build
```

## Layout
```
config/   config.json            — tolerances, claim rules, supplier mapping
schema/   canonical_schema.json  — the canonical case/line reconciliation record
data/     po_850.json            — PO (parsed EDI 850)
          asn_856.json           — ASN (parsed EDI 856)
          asn_856_raw.edi        — raw X12 856 (shows the real source format)
          grn.json               — goods receipt (WMS export)
          pod_delivery_receipt.txt — POD source content (becomes the PDF the IDP reads in Layer 1)
          ground_truth.json      — the intended answer (dev/eval reference; NOT read at runtime)
src/      (layers 1–6)
```
