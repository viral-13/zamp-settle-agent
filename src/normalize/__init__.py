"""Settle — Layer 2 normalize & resolve package.

Identity resolution (supplier item code -> buyer SKU) and unit-of-measure
cross-checks. The maintained mapping table in config is the source of truth; the
LLM may only *suggest* a candidate for an unknown code and never auto-applies one.
All arithmetic here is deterministic Python.
"""
