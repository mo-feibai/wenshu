"""Synthetic registry used only to exercise the strict gate.

The real registry (tests.baseline.known_gaps.KNOWN_GAPS) is empty: every
registered defect has been fixed. The self-tests of the gate still need a
registered ID, so they inject this synthetic table explicitly and never touch
the production registry.
"""

SYNTHETIC_GAPS = {
    "WS-008": "Synthetic placeholder for strict-gate self checks.",
}
