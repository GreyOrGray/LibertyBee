"""corpus_diff — prove two result corpora are the same physics, cell by cell, table by table.

Compares every `v1.*` table of two corpus databases over the (Rung, Seed) cells present in
both: per-cell row counts plus a checksum over every substantive column. Use it to check a
corpus you regenerated against the released one ("does my ENTIRE corpus match yours?" — the
reproduction gate re-runs sampled cells; this compares everything both corpora hold), or to
cross-validate two generations of the same sweep.

What is deliberately EXCLUDED from comparison — and why these three classes are the whole
list (naive whole-row checksums report thousands of false discrepancies from exactly these):

  1. Surrogate identity columns (detected programmatically via sys.columns.is_identity) —
     they record the order rows were extracted into each corpus, not anything about a run.
  2. EphemeralDBName — which worker database happened to execute the cell. Operational.
  3. Wall-clock stamps (StartedAtUtc / CompletedAtUtc) — when the cell ran. Operational.

Everything else must match to the byte — including simulation dates, which are
deterministic outputs of (projection, seed, engine) and therefore ARE compared.

The corpora must hold the same scenario (read from v1.corpus_meta). Comparing a standard
corpus against a deep-discount one is refused rather than reported as drift — and if either
corpus pre-dates provenance stamping the scenario cannot be verified, so the comparison is
also refused unless you assert it with --assume-same-scenario.

`v1.projection_parameters` is not keyed by cell; it is compared per ProjectionID and
classified: differences confined to naming columns (ProjectionName / Description) warn but
do not fail — they are lineage labels. A difference in any OTHER descriptor column fails
hard: parameters that differ while results match means one corpus misdescribes itself.

Usage:
  python corpus_diff.py --corpus-a MyCorpus --corpus-b LibertyBee_Released
  python corpus_diff.py --corpus-a A --server-a localhost\\FARM --corpus-b B --server-b localhost

Servers default to LB_SQL_SERVER, then localhost. Exit 0: every shared cell identical.
Exit 1: discrepancies (listed). Cells present in only one corpus are reported as coverage,
not failure — comparing a partial regeneration against a full release is a normal use.
"""
import argparse
import os
import sys

import pyodbc

DRV = os.environ.get("LB_SQL_DRIVER", "ODBC Driver 17 for SQL Server")
DEFAULT_SERVER = os.environ.get("LB_SQL_SERVER", "localhost")
VOLATILE = {"StartedAtUtc", "CompletedAtUtc", "EphemeralDBName"}


def conn(server, db):
    return pyodbc.connect(
        "DRIVER={" + DRV + "};SERVER=" + server + ";DATABASE=" + db + ";Trusted_Connection=yes")


def tables(cur):
    rows = cur.execute(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA='v1' AND TABLE_TYPE='BASE TABLE'").fetchall()
    return sorted(r[0] for r in rows)


def substantive_columns(cur, table):
    rows = cur.execute(
        "SELECT c.name FROM sys.columns c "
        "JOIN sys.tables t ON t.object_id = c.object_id "
        "JOIN sys.schemas s ON s.schema_id = t.schema_id "
        "WHERE s.name='v1' AND t.name=? AND c.is_identity=0 ORDER BY c.column_id",
        table).fetchall()
    return [r[0] for r in rows if r[0] not in VOLATILE]


def scenario_of(cur):
    try:
        row = cur.execute("SELECT TOP 1 Scenario FROM v1.corpus_meta").fetchone()
        return row[0] if row else None
    except pyodbc.Error:
        return None   # pre-provenance corpus (1.0 era)


def grouped_sums(cur, table, cols, keys):
    keylist = ", ".join(f"[{k}]" for k in keys)
    collist = ", ".join(f"[{c}]" for c in cols)
    q = (f"SELECT {keylist}, COUNT(*), CHECKSUM_AGG(BINARY_CHECKSUM({collist})) "
         f"FROM v1.[{table}] GROUP BY {keylist}")
    return {tuple(r[:len(keys)]): (r[len(keys)], r[len(keys) + 1]) for r in cur.execute(q)}


NAMING = {"ProjectionName", "Description"}


def compare_descriptors(ca, cb):
    """projection_parameters: per-ProjectionID, column-classified. Returns (hard, name_only)."""
    cols_b = set(substantive_columns(cb, "projection_parameters"))
    shared = [c for c in substantive_columns(ca, "projection_parameters") if c in cols_b]
    if "ProjectionID" not in shared:
        print("  projection_parameters: SKIP (no ProjectionID column)")
        return 0, 0
    q = f"SELECT {', '.join('[' + c + ']' for c in shared)} FROM v1.projection_parameters"
    pid_ix = shared.index("ProjectionID")
    a = {r[pid_ix]: tuple(r) for r in ca.execute(q)}
    b = {r[pid_ix]: tuple(r) for r in cb.execute(q)}
    both = sorted(set(a) & set(b))
    hard = name_only = 0
    details = []
    for pid in both:
        diffs = [c for c, x, y in zip(shared, a[pid], b[pid]) if x != y]
        if not diffs:
            continue
        if set(diffs) <= NAMING:
            name_only += 1
            details.append(f"      projection {pid}: naming only ({', '.join(diffs)}) — results unaffected")
        else:
            hard += 1
            details.append(f"      projection {pid}: DIFFERS on {', '.join(diffs)} — a corpus misdescribes itself")
    cov = f"  [coverage: {len(a) - len(both)} only in A, {len(b) - len(both)} only in B]" \
        if len(a) != len(both) or len(b) != len(both) else ""
    verdict = "IDENTICAL" if not hard and not name_only else \
        f"{hard} parameter mismatch(es), {name_only} naming-only"
    print(f"  projection_parameters (descriptors): {len(both)} shared projections -> {verdict}{cov}")
    for d in details[:8]:
        print(d)
    return hard, name_only


def compare_table(ca, cb, table, keys, label):
    cols_a = substantive_columns(ca, table)
    cols_b = set(substantive_columns(cb, table))
    shared = [c for c in cols_a if c in cols_b]
    if not all(k in shared for k in keys):
        print(f"  {table}: SKIP (no {'/'.join(keys)} keys)")
        return 0, 0
    a = grouped_sums(ca, table, shared, keys)
    b = grouped_sums(cb, table, shared, keys)
    both = set(a) & set(b)
    bad = sorted(k for k in both if a[k] != b[k])
    verdict = "IDENTICAL" if not bad else f"{len(bad)} {label}(s) DIFFER"
    extra = ""
    if len(a) != len(both) or len(b) != len(both):
        extra = f"  [coverage: {len(a) - len(both)} only in A, {len(b) - len(both)} only in B]"
    print(f"  {table}: {len(both)} shared {label}s, {len(shared)} cols -> {verdict}{extra}")
    for k in bad[:5]:
        print(f"      {k}: A={a[k]} B={b[k]}")
    return len(bad), len(both)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--corpus-a", required=True)
    p.add_argument("--corpus-b", required=True)
    p.add_argument("--server-a", default=DEFAULT_SERVER)
    p.add_argument("--server-b", default=DEFAULT_SERVER)
    p.add_argument("--assume-same-scenario", action="store_true",
                   help="proceed when a corpus pre-dates scenario stamping and same-scenario "
                        "cannot be verified — you are asserting it")
    args = p.parse_args()

    ca = conn(args.server_a, args.corpus_a).cursor()
    cb = conn(args.server_b, args.corpus_b).cursor()
    print(f"corpus_diff: A={args.server_a}/{args.corpus_a}  B={args.server_b}/{args.corpus_b}")

    sa, sb = scenario_of(ca), scenario_of(cb)
    if sa and sb and sa != sb:
        print(f"REFUSED: different scenarios (A={sa}, B={sb}) — these corpora answer "
              f"different questions and must not be diffed as drift.")
        sys.exit(2)
    if (sa is None or sb is None) and not args.assume_same_scenario:
        which = "A" if sa is None else "B"
        print(f"REFUSED: corpus {which} does not record its scenario (pre-dates provenance "
              f"stamping), so same-scenario cannot be verified. A cross-scenario diff looks "
              f"like massive drift and means nothing. If you KNOW both corpora hold the same "
              f"scenario, re-run with --assume-same-scenario.")
        sys.exit(2)
    print(f"  scenario: {sa or sb or 'unknown'}"
          + ("" if sa and sb else "  (asserted same via --assume-same-scenario)"))

    ta, tb = set(tables(ca)), set(tables(cb))
    total_bad = total_cells = 0
    for t in sorted(ta & tb):
        if t in ("corpus_meta", "projection_parameters"):
            continue
        bad, cells = compare_table(ca, cb, t, ["Rung", "Seed"], "cell")
        total_bad += bad
        total_cells = max(total_cells, cells)
    hard_desc = name_only = 0
    if "projection_parameters" in ta & tb:
        hard_desc, name_only = compare_descriptors(ca, cb)
    for t in sorted((ta | tb) - (ta & tb)):
        print(f"  {t}: only in one corpus — not compared")

    print()
    if total_bad or hard_desc:
        print(f"CORPUS DIFF FAILED: {total_bad} result discrepancies, "
              f"{hard_desc} descriptor parameter mismatches.")
        sys.exit(1)
    if name_only:
        print(f"CORPUS DIFF PASSED with a naming note: every shared cell identical across all "
              f"result tables ({total_cells} cells); {name_only} projection descriptor(s) differ "
              f"in naming columns only (lineage labels — results unaffected).")
        sys.exit(0)
    print(f"CORPUS DIFF PASSED: every shared cell identical across all shared tables "
          f"({total_cells} cells).")
    sys.exit(0)


if __name__ == "__main__":
    main()
