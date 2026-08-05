"""The fast-death / KD-042 signature constants — the single source both sides import.

Consumers with OPPOSITE dispositions on the same numbers (deliberate — do not unify):
  * corpus_checks/acquisition_binge.py (sweep side): the signature is the KNOWN class —
    a sweep halts only on fast deaths that DON'T match it.
  * living_farm/tripwires.py (publish gate, post-0.6.0): any fast death blocks publish;
    the signature only CLASSIFIES the finding (known class vs new failure mode), because
    the class was fixed in 0.6.0 and its reappearance would itself be a regression.

A pinning test asserts these exact values; editing them is a reviewed act.
"""

FAST_MONTHS = 12            # a death with ledger span < this is a "fast death"
SIG_PROPERTIES = (5, 8)     # inclusive property-count band of the KD-042 class
SIG_EVICTIONS = 0           # the class evicts nobody on the way down
