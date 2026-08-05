"""Halt the sweep on a fast death that is NOT the known over-acquisition class.

KD-042: some organisations buy more property than their cash can carry, then die
young — with 5-8 properties and no evictions, because they fail before any tenancy
has time to go bad. That pattern is understood and expected in the corpus.

A fast death OUTSIDE that signature is a different animal, and the reason to stop:
continuing would spend days generating runs against an engine whose failure mode we
do not yet understand, and the corpus would be discarded anyway. Better to find out
at run 250 than run 8,000.

This check encodes a Liberty Bee-specific finding. If you are running your own
scenarios, disable it in checks.json — it will produce noise that means nothing.
"""

DESCRIPTION = "fast deaths off the known KD-042 over-acquisition signature (HALTS)"

from signature_constants import FAST_MONTHS, SIG_PROPERTIES, SIG_EVICTIONS  # single source (see that module's dual-disposition note)


def check(cursor, ctx):
    rows = cursor.execute(f"""
        SELECT rs.Rung, rs.Seed, rs.PropertyCount, rs.EvictionCount,
               (SELECT DATEDIFF(MONTH, MIN(fl.LedgerDate), MAX(fl.LedgerDate))
                FROM v1.fund_ledger fl
                WHERE fl.Rung = rs.Rung AND fl.Seed = rs.Seed) AS Span
        FROM v1.run_summary rs
        WHERE rs.Survived = 0
    """).fetchall()

    fast = [r for r in rows if (r.Span if r.Span is not None else 999) <= FAST_MONTHS]
    lo, hi = SIG_PROPERTIES
    off = [r for r in fast
           if not (lo <= (r.PropertyCount or 0) <= hi
                   and (r.EvictionCount or 0) == SIG_EVICTIONS)]

    summary = (f"{len(fast)} fast deaths, {len(off)} off the KD-042 signature "
               f"({lo}-{hi} properties, {SIG_EVICTIONS} evictions)")
    if not off:
        return {"summary": summary, "halt": False}

    detail = ", ".join(
        f"rung{r.Rung} seed{r.Seed} (span={r.Span}mo, props={r.PropertyCount}, "
        f"evictions={r.EvictionCount})" for r in off[:8])
    if len(off) > 8:
        detail += f", and {len(off) - 8} more"

    return {
        "summary": summary + "  *** NEW FAILURE CLASS ***",
        "halt": True,
        "detail": ("These deaths do not match the known over-acquisition pattern. "
                   "Investigate before generating more:\n    " + detail),
    }
