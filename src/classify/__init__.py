"""Settle — Layer 4 liability classification package.

The judgment step. The LLM reasons over the assembled evidence + the GENERAL policy
and reaches the liable party itself. It must never recompute or alter a quantity or
dollar value (those are fixed by Layer 3), and ground_truth.json is never fed to it.
"""
