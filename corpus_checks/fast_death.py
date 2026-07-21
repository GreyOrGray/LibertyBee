"""Report organisations that died implausibly early.

Never halts on its own — a fast death is not inherently wrong (an under-funded rung
is *supposed* to fail), and at the low rungs most runs die. This is the smoke alarm:
it makes the death profile visible while the sweep runs, so a regression that kills
organisations everywhere shows up on the next checkpoint rather than at the end.

`acquisition_binge` is the check that decides whether fast deaths are the KNOWN
failure class or a new one.
"""

DESCRIPTION = "death count and how many died within 12 months"

FAST_MONTHS = 12


def check(cursor, ctx):
    total = cursor.execute("SELECT COUNT(*) FROM v1.run_summary").fetchone()[0]
    if not total:
        return {"summary": "no runs extracted yet", "halt": False}

    deaths = cursor.execute(
        "SELECT COUNT(*) FROM v1.run_summary WHERE Survived = 0").fetchone()[0]

    # Lifespan from the fund ledger: the corpus has no duration column, and the
    # ledger spans exactly the months the organisation was alive.
    fast = cursor.execute(f"""
        SELECT COUNT(*) FROM v1.run_summary rs
        WHERE rs.Survived = 0
          AND (SELECT DATEDIFF(MONTH, MIN(fl.LedgerDate), MAX(fl.LedgerDate))
               FROM v1.fund_ledger fl
               WHERE fl.Rung = rs.Rung AND fl.Seed = rs.Seed) <= {FAST_MONTHS}
    """).fetchone()[0]

    pct = (deaths / total * 100.0) if total else 0.0
    return {
        "summary": (f"{total} runs, {deaths} deaths ({pct:.1f}%), "
                    f"{fast} within {FAST_MONTHS}mo"),
        "halt": False,
    }
