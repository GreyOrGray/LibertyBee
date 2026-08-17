"""corpus_runner — the generic Monte Carlo run harness, shared by both callers.

This is the store-AGNOSTIC core: the worker-database pool, the sim invocation,
projection resolution from seeded data, provenance, pacing, checks, and the
dispatcher loop. It knows nothing about where results land — a Store (passed in)
decides that. The corpus CLI (regenerate_corpus.py) supplies a CorpusStore that
writes a full v1.* extract; the living farm supplies its own Store. Same runner,
two consumers.

Per-(rung, seed) lifecycle (run_sweep -> process_pair):
    1. store.already_done(rung, seed) -> skip if recorded (restartable).
    2. build a fresh worker database from the Gold/Silver baseline.
    3. store.prepare_worker: mode flip (no-op on regime) + verify the rung is the
       seeded projection the run resolved at startup.
    4. run `simulation.py --env <db> --projection-id <rung> --seed <seed>` as a
       subprocess.
    5. on exit 0: store.record(...) the result; return the worker to the pool.
       on exit != 0: log and retain the worker for inspection.

A "rung" is a projection. Which projections run is user-named; every one is seeded
data (V00072+), and resolve_rungs reads each one's funding amount and affordability
scenario from that data — never a hardcoded map. Run-state (target DB, the
rung->funds map, scenario, inflation leg) lives on the Store, not in module
globals, so two Stores can be built without interfering.

Parallelism: one dispatcher thread hands cells to N worker threads, each running
the sim as a separate OS subprocess — real parallelism across cores, not GIL-bound.
"""

import argparse
import collections
import concurrent.futures
import datetime
import importlib.util
import json
import os
import pathlib
import socket
import subprocess
import sys
import time

import pyodbc

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Engine version of record, stamped into every corpus row and v1.corpus_meta.
# 0.6.0 = the V2 engine (Gray-ruled 2026-07-21, confirming the fork's provisional
# label). Decided BEFORE the sweep because the stamp is per-row and permanent —
# the spot-check caught the vendored 1.0 value (0.5.0) still here, the one
# column out of 25 cells x ~25 metrics that differed from the 8k baseline.
# live_store.ENGINE_VERSION mirrors this for the farm's stamp; keep them in step.
ENGINE_VERSION = "0.6.0"

# Connections come from the shared backend-selected layer (D3,
# pg_corpus_harness_design.md): corpus_conn.connect() serves pyodbc or
# qmark-wrapped psycopg per LB_CORPUS_BACKEND / --pg. The names below are
# re-exported so existing `from corpus_runner import CONN_TMPL, conn`
# importers keep working.
from corpus_conn import (CONN_TMPL, SQL_SERVER, SQL_DRIVER,  # noqa: F401
                         connect as _cc_connect)

# Run-state (target corpus DB, the rung->funds map, scenario, inflation leg) is NOT
# module-global: it lives on the Store (see CorpusStore), so the generic runner
# holds no state and two Stores can be built without interfering. The rung->funds
# map and scenario are read from the SEEDED DATA by resolve_rungs() at startup —
# never a hardcoded ladder. The corpus's Rung column is the funding amount, so the
# same funding level under two scenarios stays comparable rung-for-rung even though
# the projection ids differ.

# The script ships at the repository root; all paths are resolved relative to it.
# Deliberately self-relative: the harness must run the engine of the tree it was
# promoted into, never a tree named by an absolute path baked in elsewhere.
REPO = os.path.dirname(os.path.abspath(__file__))
SIM_SCRIPT = os.path.join(REPO, "app", "src", "simulation.py")
STATUS_FILE = os.path.join(REPO, "corpus_regen_status.txt")
LOG_DIR = os.path.join(REPO, "corpus_regen_logs")

# Touch this file to drain a running sweep gracefully (in-flight sims finish and
# extract; nothing new starts). Removing it does NOT resume — re-invoke, and the
# already-done skip makes the sweep pick up where it stopped.
STOP_FLAG = pathlib.Path(REPO) / "sweep.stop"

DEFAULT_WORKERS = 8
DEFAULT_MONTHS = 240

# Fixed per-slot worker DB names, reset to Gold per sim via migration_manager
# --envname (ephemeral-prefix-guarded, re-stamps provenance). A fixed pool of
# names avoids the mint-counter race that concurrent fresh mints hit (all slots
# computing the same next number). A failed sim RETAINS its slot DB for
# inspection (the pool shrinks).
import queue as _queue
WORKER_NAME_POOL = _queue.Queue()

def init_worker_pool(n):
    import corpus_conn as _cc
    # PG slots are 'pgcw', not a lowercase 'cw': Windows' case-insensitive
    # filesystem makes environments/libertybee_test_cw00 the SAME folder as
    # environments/LibertyBee_Test_cw00, so case-only distinction let a PG
    # worker config shadow the SQL Server one (caught by verify_worker_config
    # fail-loud on the first dual-engine day).
    prefix = "libertybee_test_pgcw" if _cc.is_pg() else "LibertyBee_Test_cw"
    for i in range(n):
        WORKER_NAME_POOL.put(f"{prefix}{i:02d}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def conn(db_name):
    return _cc_connect(db_name, timeout=30, autocommit=True)


# A corpus of record is generated from a PROMOTED checkout, never from the
# development tree. Identified by origin: development work lives in the -Dev
# repository, and a corpus built there cannot correspond to anything published.
# A third party's origin matches neither marker, so this never fires for them.
DEV_REPO_MARKER = "LibertyBeeDev"


CHECKS_DIR = os.path.join(REPO, "corpus_checks")


def load_checks(names=None):
    """Load enabled corpus checks from corpus_checks/. Returns [(name, module)].

    `names` overrides checks.json: a list, ['all'], or [] for none. A check that
    fails to import is reported and skipped rather than aborting — a broken check
    must never prevent a sweep from starting.
    """
    if not os.path.isdir(CHECKS_DIR):
        return []

    available = sorted(
        f[:-3] for f in os.listdir(CHECKS_DIR)
        if f.endswith(".py") and not f.startswith("_"))

    if names is None:
        cfg_path = os.path.join(CHECKS_DIR, "checks.json")
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                names = json.load(fh).get("enabled", [])
        except (OSError, ValueError) as e:
            print(f"  [checks] could not read {cfg_path} ({e}); no checks enabled",
                  flush=True)
            return []
    if names == ["all"]:
        names = available

    # checks may import siblings (e.g. signature_constants) — the file-location
    # loader gives them no package context, so the checks dir must be on sys.path
    if CHECKS_DIR not in sys.path:
        sys.path.insert(0, CHECKS_DIR)

    loaded = []
    for name in names:
        if name not in available:
            print(f"  [checks] '{name}' not found in {CHECKS_DIR} "
                  f"(available: {', '.join(available) or 'none'})", flush=True)
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"corpus_checks.{name}", os.path.join(CHECKS_DIR, f"{name}.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if not hasattr(mod, "check"):
                print(f"  [checks] '{name}' has no check() function; skipping", flush=True)
                continue
            loaded.append((name, mod))
        except Exception as e:
            print(f"  [checks] '{name}' failed to load ({e}); skipping", flush=True)
    return loaded


def run_checks(checks, ctx, store):
    """Run enabled checks against the store's database. Returns the name of the
    first check that asked to halt, else None.

    Each check is isolated: an exception is reported and the sweep continues.
    Losing days of compute to a bug in a monitoring script would be absurd.
    """
    halting = None
    for name, mod in checks:
        try:
            with store.central_conn() as c:
                result = mod.check(c.cursor(), ctx) or {}
        except Exception as e:
            print(f"  [check:{name}] ERROR {type(e).__name__}: {e} (sweep continues)",
                  flush=True)
            continue
        print(f"  [check:{name}] {result.get('summary', '(no summary)')}", flush=True)
        if result.get("halt"):
            halting = halting or name   # first check to ask wins the attribution
            if result.get("detail"):
                print(f"  [check:{name}] {result['detail']}", flush=True)
    return halting


def harness_provenance():
    """What produced this corpus: (commit_sha, is_dirty, repo_root, origin_url).

    Read from git in REPO — the tree this script is actually running from, not a
    configured path — so it reports the code that genuinely ran. Returns
    commit=None, dirty=False when REPO is not a git checkout (e.g. a third party
    running from a downloaded archive): unknown provenance is recorded honestly
    as unknown, and only a POSITIVELY detected modification is treated as dirty.
    """
    def git(*args):
        try:
            out = subprocess.run(["git", "-C", REPO, *args],
                                 capture_output=True, text=True, timeout=30)
            return out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    commit = git("rev-parse", "HEAD")
    if commit is None:
        return None, False, REPO, None
    status = git("status", "--porcelain")
    return commit, bool(status), REPO, git("remote", "get-url", "origin")


def parse_int_set(spec):
    """Parse a mixed list of integers and inclusive ranges into a sorted, deduped
    list: '100,125,130-150,200-210' -> [100, 125, 130, ..., 150, 200, ..., 210].

    One parser for both rungs and seeds — the two axes take the same grammar, and
    having a second, weaker parser (the old comma-only parse_rungs) was a small
    instance of the same duplication the unified runner removes. A set dedupes, so
    overlapping ranges and repeats ('100,100') are harmless.
    """
    if not spec or not spec.strip():
        raise ValueError("empty integer-set specification")
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part.lstrip("-"):  # a range, not a leading minus
            lo, hi = part.split("-", 1)
            lo, hi = int(lo), int(hi)
            if hi < lo:
                raise ValueError(f"range '{part}' has its end below its start")
            out.update(range(lo, hi + 1))
        else:
            out.add(int(part))
    return sorted(out)


# Back-compat alias: --seeds has always used this grammar.
parse_seed_range = parse_int_set


def build_worker_db():
    """Take a slot name from the pool and destructively reset it to
    Gold+migrations via migration_manager --envname (subprocess: its logging
    setup clashes with threaded stdout). Returns the DB name. On failure the
    slot name is NOT returned to the pool (retained for inspection)."""
    import corpus_conn as _cc
    name = WORKER_NAME_POOL.get(timeout=3600)
    cmd = [sys.executable, "environmentscripts/migration_manager.py", "--envname", name]
    if _cc.is_pg():
        # named template reset (the sweep's template rides LB_PG_TEMPLATE —
        # for the supersede that is the A2 salem bake, not the engine gold)
        cmd.insert(2, "--pg")
    proc = subprocess.run(
        cmd,
        capture_output=True, text=True, cwd=REPO, timeout=900,
    )
    if proc.returncode != 0:
        tail = (proc.stdout or "")[-300:] + (proc.stderr or "")[-200:]
        raise RuntimeError(f"migration_manager --envname {name} exit {proc.returncode}: {tail}")
    verify_worker_config(name)
    tune_worker_db(name)
    return name


def verify_worker_config(db_name):
    """The sim decides where to connect by reading environments/<db>/db_config.json,
    which migration_manager writes only on FIRST create — so a stale file from an
    earlier run on a different instance silently points the sim at the WRONG
    server: the harness restores the worker on SQL_SERVER while the sim runs
    against an old same-named database elsewhere. Caught for real on 2026-07-21
    (first named-instance rehearsal: two workers' sims hit the default instance —
    one crashed on the old schema, one SUCCEEDED against the wrong database and
    was only caught by the missing Run row). The farm path always had this guard;
    the corpus path only ever ran on the default instance, where the stale config
    was accidentally correct. Fail loud on any mismatch."""
    import corpus_conn as _cc
    cfg_path = os.path.join(REPO, "environments", db_name, "db_config.json")
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    expected_server = _cc.PG_HOST if _cc.is_pg() else SQL_SERVER
    if cfg.get("server", "").upper() != expected_server.upper():
        raise RuntimeError(
            f"{cfg_path} points at server '{cfg.get('server')}', expected "
            f"'{expected_server}'. Stale config from an earlier run on a different "
            f"instance — delete the environment folder and retry.")
    if _cc.is_pg() and cfg.get("backend") != "psycopg":
        raise RuntimeError(
            f"{cfg_path} backend is '{cfg.get('backend')}', expected 'psycopg' — "
            f"a stale SQL Server config would point the sim at the wrong engine.")
    if cfg.get("database") != db_name:
        raise RuntimeError(
            f"{cfg_path} database is '{cfg.get('database')}', expected '{db_name}'")


def tune_worker_db(db_name):
    """Configure a worker for throwaway use.

    Workers are the ONLY databases here that may be SIMPLE. They are disposable
    by construction — reset to Gold before every sim, never backed up, never a
    source of truth — so there is no log chain to protect. Left in the recovery
    model inherited from Gold, their logs would grow across thousands of restores
    with nothing ever truncating them.

    Corpora are the opposite: durable artifacts, created FULL by create_corpus.py.
    Never apply this to a corpus.

    Best-effort: a worker that will not take the tune still runs correctly, just
    noisier on disk, so a failure here must not abort the sweep.
    """
    import corpus_conn as _cc
    if not db_name.lower().startswith("libertybee_test_"):
        raise RuntimeError(
            f"refusing to apply throwaway tuning to '{db_name}' — not an ephemeral "
            f"LibertyBee_Test_* database")
    try:
        if _cc.is_pg():
            # the PG analogue of DELAYED_DURABILITY: async commit for
            # throwaway workers — a crash reruns the sim; the corpus (a
            # different database, untouched here) keeps full durability
            with _cc.connect("postgres") as c:
                c.execute(f"ALTER DATABASE {db_name} SET synchronous_commit = off")
        else:
            with conn("master") as c:
                cur = c.cursor()
                cur.execute(f"ALTER DATABASE [{db_name}] SET RECOVERY SIMPLE")
                # Worker durability is irrelevant: if the box dies mid-sim the run is
                # rerun from scratch anyway, and the corpus (a different database) is
                # untouched. Trading fsyncs for speed here costs nothing recoverable.
                cur.execute(f"ALTER DATABASE [{db_name}] SET DELAYED_DURABILITY = FORCED")
    except _cc.db_errors() as e:
        print(f"  [warn] tune_worker_db({db_name}) failed, continuing: {e}", flush=True)


def release_worker_db(db_name):
    """Return a slot to the pool for the next (rung, seed). The DB is NOT
    dropped between sims - the next reset-to-Gold wipes it."""
    WORKER_NAME_POOL.put(db_name)


def drop_worker_db(db_name):
    """Drop an ephemeral worker DB."""
    import corpus_conn as _cc
    try:
        if _cc.is_pg():
            with _cc.connect("postgres") as c:
                c.execute(f"DROP DATABASE IF EXISTS {db_name}")
        else:
            with conn("master") as c:
                cur = c.cursor()
                cur.execute(f"ALTER DATABASE [{db_name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
                cur.execute(f"DROP DATABASE [{db_name}]")
    except Exception as e:
        print(f"  [warn] drop_worker_db({db_name}) failed: {e}", flush=True)




def apply_sweep_mode(worker_db, sweep_mode):
    """Static leg: flip the WORKER's default INF.Mode row to Static (the worker
    is per-sim disposable, so the default flip scopes exactly one sim). Fail-loud
    if the row shape is unexpected. No-op on the default 'regime' leg.

    NOTE: targets reference.ParameterRegistryDefault (the old reference.ParameterRegistry
    was dropped at V00070). The static leg is unexercised by the V2 corpora (both
    regime), so this path is not covered by the anchor.
    """
    if sweep_mode != "static":
        return
    with conn(worker_db) as w:
        cur = w.cursor()
        cur.execute("""
            UPDATE reference.ParameterRegistryDefault SET Value = 'Static'
            WHERE Category = 'INF' AND Name = 'Mode'
        """)
        if cur.rowcount != 1:
            raise RuntimeError("static-mode flip touched %d rows on %s (expected 1)" % (cur.rowcount, worker_db))


def run_sim(worker_db, rung, seed, months, sweep_mode):
    """Invoke simulation.py as a subprocess. Returns (exit_code, started_utc, completed_utc, log_path)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"sim_{sweep_mode}_r{rung}_s{seed}.log")
    started = datetime.datetime.utcnow()
    with open(log_path, "w") as log:
        proc = subprocess.run(
            [sys.executable, str(SIM_SCRIPT),
             "--env", worker_db,
             "--projection-id", str(rung),
             "--seed", str(seed),
             "--months", str(months)],
            stdout=log, stderr=subprocess.STDOUT,
            cwd=REPO,
        )
    completed = datetime.datetime.utcnow()
    return proc.returncode, started, completed, log_path


class Store:
    """Interface. A Store owns everything store-specific about a run: its target
    database, the rung->funds map, the scenario/mode, how a completed run is
    recorded, and how "already done" is judged. It carries the run-state the
    generic runner used to keep in module globals.
    """
    central_db = None
    scenario = None
    sweep_mode = None
    rung_funds = None    # projection id -> funding amount; set by resolve()

    # --- results ------------------------------------------------------------
    def central_conn(self):
        """A connection to the result database, on this store's instance."""
        raise NotImplementedError

    def already_done(self, rung, seed):
        raise NotImplementedError

    def prepare_worker(self, worker_db, rung):
        """Ready a freshly-built worker for this rung (mode flip + verify)."""
        raise NotImplementedError

    def record(self, worker_db, rung, seed, run_id, started_utc, completed_utc, months):
        raise NotImplementedError

    def clear(self, rung, seed):
        raise NotImplementedError

    def bind(self, allow_dirty=False, allow_dev_tree=False):
        """Bind the run to its store (scenario/provenance) and return
        (commit, dirty). Called once before the run starts."""
        raise NotImplementedError

    # --- workers ------------------------------------------------------------
    # The Store also owns WHERE ITS WORKERS RUN (design B, Gray-ruled
    # 2026-07-21): which SQL instance, which migration_manager, which sim
    # script, and its own safety rules. The corpus runs workers beside Gold on
    # the default instance; the farm runs them on a dedicated instance whose
    # guard REFUSES any instance hosting Gold/Prod. Making the backend
    # store-owned keeps each deployment's safety with its Store instead of
    # depending on an environment variable being set before import.

    def build_worker(self):
        """Build/reset a worker DB from the baseline; return its name."""
        raise NotImplementedError

    def release_worker(self, worker_db):
        raise NotImplementedError

    def worker_conn(self, worker_db):
        """A connection to a worker DB on this store's instance."""
        raise NotImplementedError

    def run_sim(self, worker_db, rung, seed, months):
        """Run the engine; return (exit_code, started_utc, completed_utc, log_path)."""
        raise NotImplementedError

    def worker_run_id(self, worker_db):
        """RunID of the finished sim in the worker (always 1 for fresh-DB-per-sim)."""
        with self.worker_conn(worker_db) as c:
            row = c.cursor().execute("SELECT MAX(RunID) FROM simulation.Run").fetchone()
        return row[0] if row else None

    # --- ladder resolution (concrete — same logic for every store) ----------
    def read_projection(self, worker_db, projection_id):
        """(scenario_tag, starting_funds) for a projection from the seeded data,
        or None if it does not exist."""
        with self.worker_conn(worker_db) as w:
            return w.cursor().execute("""
                SELECT p.ScenarioTag, d.Value
                FROM reference.Projection p
                LEFT JOIN reference.ParameterRegistryDefined d
                  ON d.ProjectionID = p.ProjectionID
                 AND d.Category = 'FIN' AND d.Name = 'StartingFunds'
                WHERE p.ProjectionID = ?
            """, (projection_id,)).fetchone()

    def resolve(self, rung_ids, scenario_declared=None):
        """Resolve the named projections against the SEEDED DATA using this
        store's own workers: funding amounts + the single shared scenario. Sets
        self.rung_funds and self.scenario. Fails fast — an unknown projection,
        missing funds, or a mix of scenarios is caught here, before a single sim
        runs. The probe doubles as an early check that the engine even builds
        from this store's baseline.
        """
        probe = self.build_worker()
        try:
            funds, tags = {}, {}
            for pid in rung_ids:
                row = self.read_projection(probe, pid)
                if row is None:
                    raise SystemExit(
                        f"REFUSING: projection {pid} does not exist in a fresh environment. "
                        f"Seeded projections come from the migration chain (V00072+); name "
                        f"projections that exist, or add a migration that seeds this one.")
                tag, f = row
                if f is None:
                    raise SystemExit(f"REFUSING: projection {pid} has no FIN.StartingFunds.")
                funds[pid] = float(f)
                tags[pid] = tag
        finally:
            self.release_worker(probe)

        scenarios_found = sorted(set(tags.values()))
        if len(scenarios_found) > 1:
            by = {s: [p for p in rung_ids if tags[p] == s] for s in scenarios_found}
            raise SystemExit(
                f"REFUSING: the named projections span {len(scenarios_found)} scenarios {by} — "
                f"a corpus holds exactly one. Run each scenario as its own corpus.")
        resolved = scenarios_found[0]
        if scenario_declared and scenario_declared != resolved:
            raise SystemExit(
                f"REFUSING: --scenario {scenario_declared!r} was given, but the named "
                f"projections are all '{resolved}'. Drop --scenario, or name projections "
                f"tagged '{scenario_declared}'.")
        self.rung_funds = funds
        self.scenario = resolved
        return funds, resolved

    def verify_projection(self, worker_db, rung):
        """Confirm this WORKER's copy of the rung matches what resolve() read at
        startup — same funds, same scenario. resolve() validated existence against
        a probe; this per-worker check catches a worker built from a DIFFERENT
        baseline than the probe (e.g. a stale Gold), which would otherwise produce
        plausible numbers for the wrong configuration."""
        expected_funds = self.rung_funds[rung]
        row = self.read_projection(worker_db, rung)
        if row is None:
            raise RuntimeError(
                f"projection {rung} does not exist in {worker_db} — this worker was "
                f"built from a baseline that predates it, unlike the startup probe.")
        scenario_tag, funds = row
        if funds is None:
            raise RuntimeError(f"projection {rung} has no FIN.StartingFunds in {worker_db}")
        if abs(float(funds) - float(expected_funds)) > 0.005:
            raise RuntimeError(
                f"projection {rung} has StartingFunds {funds} in {worker_db}, but startup "
                f"resolved {expected_funds:.2f} — this worker's baseline disagrees with "
                f"the probe's.")
        if scenario_tag != self.scenario:
            raise RuntimeError(
                f"projection {rung} is tagged scenario '{scenario_tag}' in {worker_db} but "
                f"the run resolved to '{self.scenario}' — this worker's baseline disagrees "
                f"with the probe's.")


def process_pair(rung, seed, months, store, dry_run=False):
    """The full lifecycle for one (rung, seed). Runs on a worker thread.

    `store` decides skip-if-done and how the result is recorded; everything else
    (worker build, projection verify, sim) is store-agnostic.
    """
    result = {"rung": rung, "seed": seed, "status": "pending",
              "worker_db": None, "wall_sec": None, "error": None}

    if dry_run:
        result["status"] = "dry_run"
        return result

    if store.already_done(rung, seed):
        result["status"] = "skipped (already recorded)"
        return result

    t0 = time.time()
    try:
        worker_db = store.build_worker()
        result["worker_db"] = worker_db
    except Exception as e:
        result["status"] = "failed (build_worker)"
        result["error"] = str(e)
        return result

    try:
        store.prepare_worker(worker_db, rung)  # mode flip (no-op on regime) + verify the rung
    except Exception as e:
        result["status"] = "failed (worker prep); worker DB retained"
        result["error"] = str(e)
        return result

    exit_code, started_utc, completed_utc, log_path = store.run_sim(
        worker_db, rung, seed, months)
    if exit_code != 0:
        result["status"] = f"failed (sim exit {exit_code}); worker DB retained"
        result["error"] = f"see log: {log_path}"
        return result

    # Sim succeeded — extract and drop.
    try:
        run_id = store.worker_run_id(worker_db)
        if run_id is None:
            result["status"] = "failed (no Run row in worker DB after sim)"
            return result

        store.record(worker_db, rung, seed, run_id, started_utc, completed_utc, months)
        store.release_worker(worker_db)
        result["status"] = "completed"
    except Exception as e:
        result["status"] = "failed (extract); worker DB retained"
        result["error"] = str(e)
        return result
    finally:
        result["wall_sec"] = round(time.time() - t0, 1)

    return result


# ---------------------------------------------------------------------------
# Sweep orchestration
# ---------------------------------------------------------------------------

def write_status(stats, pending, completed, failed, started, target,
                 halted_by=None, drained=False):
    elapsed = (time.time() - started) / 60.0
    pct = (completed + failed) / target * 100.0 if target else 0
    eta_min = (elapsed / max(completed, 1)) * pending if completed > 0 else None
    lines = [
        f"Corpus regeneration — status as of {datetime.datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Target sims:    {target}",
        f"Completed:      {completed}",
        f"Failed:         {failed}",
        f"Pending:        {pending}",
        f"Skipped:        {stats.get('skipped', 0)}",
        f"Progress:       {pct:.1f}%",
        f"Elapsed:        {elapsed:.1f} min",
        f"ETA:            {eta_min:.1f} min" if eta_min else "ETA:            N/A",
    ]
    # Surfaced here, not only on stdout: an unattended sweep is read from this
    # file, and a halt that only ever printed to a closed terminal is a halt
    # nobody acts on.
    if halted_by:
        lines += ["",
                  f"*** HALTED BY CHECK: {halted_by} ***",
                  "The sweep stopped early because a check reported a condition worth",
                  "investigating before generating more runs. The corpus is INCOMPLETE",
                  "and should not be frozen or cited until the finding is resolved."]
    elif drained:
        lines += ["", "Drained via sweep.stop — corpus is INCOMPLETE but consistent;",
                  "re-invoke the same command to resume."]
    lines += ["", "Recent failures (worker DB retained for inspection):"]
    for f in stats.get("failures", [])[-10:]:
        lines.append(f"  rung={f['rung']} seed={f['seed']}: {f['status']}; worker={f['worker_db']}; error={f.get('error', '')}")
    with open(STATUS_FILE, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def run_sweep(pairs, workers, months, store, dry_run=False, force_rerun=False,
              pacer=None, drip_gap=20.0, checks=(), check_every=250):
    """Run the sweep across all (rung, seed) pairs.

    Concurrency is re-decided each second against a target: `workers` normally,
    or whatever `pacer` dictates (0 / 1 / workers) when throttling. Throttling
    changes only WHEN work is submitted, never what is computed — every sim is
    an independent seeded run in its own database, so pacing cannot affect
    results.

    A `sweep.stop` file beside this script drains gracefully: in-flight sims
    finish and are extracted, nothing new is submitted. That matters for a
    multi-day sweep, where killing the process would strand worker databases
    and lose the runs in progress.
    """
    started = time.time()
    if force_rerun:
        for rung, seed in pairs:
            store.clear(rung, seed)

    stats = {"skipped": 0, "failures": []}
    completed = 0
    failed = 0
    target_total = len(pairs)

    print(f"Starting sweep: {target_total} pairs, {workers}-parallel, "
          f"throttle={'on' if pacer else 'off'}, dry_run={dry_run}", flush=True)

    work = collections.deque(pairs)
    in_flight = {}
    last_completion = 0.0
    mode_logged = None
    drained = False
    halted_by = None

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        while work or in_flight:
            # --- reap anything finished -------------------------------------
            for fut in [f for f in in_flight if f.done()]:
                r, s = in_flight.pop(fut)
                try:
                    result = fut.result()
                except Exception as e:
                    result = {"rung": r, "seed": s, "status": "failed (exception)",
                              "error": str(e), "worker_db": None}

                if "skipped" in result["status"]:
                    stats["skipped"] += 1
                    completed += 1
                elif result["status"] in ("completed", "dry_run"):
                    completed += 1
                    last_completion = time.monotonic()
                else:
                    failed += 1
                    stats["failures"].append(result)

                pending = target_total - completed - failed
                print(f"  [{completed+failed}/{target_total}] rung={r} seed={s}: "
                      f"{result['status']} ({result.get('wall_sec', '?')}s)", flush=True)
                write_status(stats, pending, completed, failed, started, target_total)

                # Checkpoint: let enabled checks look at the corpus so far.
                if checks and not dry_run and completed and completed % check_every == 0:
                    ctx = {"completed": completed, "failed": failed,
                           "target": target_total, "scenario": store.scenario,
                           "sweep_mode": store.sweep_mode}
                    halting = run_checks(checks, ctx, store)
                    if halting and work:
                        print(f"  check '{halting}' asked to HALT — draining; in-flight "
                              f"runs finish and extract, nothing new starts.", flush=True)
                        work.clear()
                        halted_by = halting

            # --- graceful drain ---------------------------------------------
            if STOP_FLAG.exists() and work:
                print(f"  {STOP_FLAG.name} present — draining; in-flight sims will "
                      f"finish and extract, nothing new submitted.", flush=True)
                work = []
                drained = True

            # --- decide concurrency -----------------------------------------
            if pacer is None:
                target = workers
            else:
                mode, reason = pacer.decide()
                if mode != mode_logged:
                    print(f"  [throttle] -> {mode} ({reason})", flush=True)
                    mode_logged = mode
                target = {"pause": 0, "drip": 1, "blast": workers}[mode]
                # In drip, leave a gap between runs rather than starting the next
                # the instant one finishes.
                if mode == "drip" and in_flight and \
                        (time.monotonic() - last_completion) < drip_gap:
                    target = 0

            while work and len(in_flight) < target:
                rung, seed = work.popleft()
                in_flight[ex.submit(process_pair, rung, seed, months, store, dry_run)] = (rung, seed)

            # Wait for actual progress rather than polling on a fixed tick. A
            # resumed sweep is mostly already-done pairs that skip in
            # milliseconds; a blind 1s sleep would drain those at `workers` per
            # second (~16 min to re-walk 8,000 pairs). The 1s cap keeps the
            # pacer responsive when runs are genuinely long.
            if in_flight:
                concurrent.futures.wait(
                    list(in_flight), timeout=1.0,
                    return_when=concurrent.futures.FIRST_COMPLETED)
            elif work:
                time.sleep(1.0)   # pacer holding at target=0 with nothing running

    # Final pass so a short sweep (fewer than check_every runs) is still checked.
    if checks and not dry_run and completed:
        print("  final checks:", flush=True)
        run_checks(checks, {"completed": completed, "failed": failed,
                            "target": target_total, "scenario": store.scenario,
                            "sweep_mode": store.sweep_mode}, store)

    if halted_by:
        outcome = f"HALTED BY CHECK ({halted_by})"
    elif drained:
        outcome = "DRAINED (sweep.stop)"
    else:
        outcome = "complete"
    elapsed_min = (time.time() - started) / 60.0
    write_status(stats, 0, completed, failed, started, target_total,
                 halted_by=halted_by, drained=drained)
    print(f"\nSweep {outcome}: {completed} done ({stats['skipped']} skipped), "
          f"{failed} failed in {elapsed_min:.1f} min", flush=True)
    return stats, completed, failed, halted_by


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

