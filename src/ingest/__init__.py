"""Settle — Layer 1 ingestion package.

Module split (deliberate, per the non-negotiable rule):
  parsers.py        — DETERMINISTIC only. EDI 850/856 + GRN parsing. No LLM.
  pod_idp.py        — THE ONLY LLM step. POD fact extraction from a PDF.
  build_evidence.py — DETERMINISTIC merge of the above into the canonical case.
"""
