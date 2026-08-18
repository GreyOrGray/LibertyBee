"""Liberty Bee cockpit — a local app to configure and run your own region.

The adopter's control surface. Two config sections + run + results:
  * Section 1 — TOWN KNOBS: per-town local-market params (income / tax / insurance / ...) ->
    reference.TownParameterOverride. The engine resolves each property by its town
    (Scope='local').
  * Section 2 — PARAMETER SETS: the model-mechanics knobs, via the engine's projection
    mechanism (a projection = a named parameter set; run-selected by --projection-id).
    The shipped canonical sets are read-only — clone to experiment.
  * RUN — launch a simulation against an environment and watch its progress.
  * RESULTS — the run's verdict, the Cash/CSF series, and headline counts.

DB-backed and LOCAL: it talks to your LibertyBee database via the engine's DatabaseManager,
binds to 127.0.0.1 only, and never phones anywhere.
Run:  python cockpit/app.py   then open  http://127.0.0.1:5000
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

from flask import Flask, render_template, request, redirect, url_for, abort

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "app", "src"))
from database_manager import DatabaseManager  # noqa: E402

app = Flask(__name__)

# Cockpit-created parameter sets live at ProjectionID >= CLONE_BASE; everything
# below is shipped canon and is never editable here (enforced in UI AND server-side).
CLONE_BASE = 9000

RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")
_REG_LOCK = threading.Lock()

# The local-market "town knobs" surfaced in Section 1 (a subset of the Scope='local' params — the
# ones an adopter actually varies by town). label + a short hint. Tax/insurance/income/vacancy are
# all town-resolved in the engine.
TOWN_KNOBS = [
    ("INC", "EarnerMedianAnnual", "Median renter earner income", "$/yr",
     "Median annual income of an individual earner in your local renter pool. The engine calibrates "
     "the emergent household-income mix around this (source: US Census ACS B25119, renter-occupied)."),
    ("OPEX", "PropertyTaxPerUnit", "Property tax", "$/unit/yr",
     "Annual property tax per unit — your municipality's residential rate × the per-unit assessed value."),
    ("OPEX", "InsurancePerUnit", "Insurance", "$/unit/yr",
     "Annual building insurance per unit — e.g. a local small-building (BOP) quote."),
    ("PROP", "VacancyRateBase", "Your operator's own vacancy", "fraction",
     "The share of YOUR units sitting empty in a normal market — your operator's OWN steady-state "
     "vacancy, which sits below the broader market rate because desirable/affordable units fill faster. "
     "No own figure? Your local market vacancy is a reasonable proxy (that's what the Salem example "
     "uses — 1.9%). Not a downturn lever; cyclical vacancy is modeled separately."),
]


# ---------------------------------------------------------------- environments

def _env_dir():
    return os.path.join(REPO, "environments")


def _regions():
    """PG environments only — the cockpit is a v3 surface."""
    d = _env_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for x in sorted(os.listdir(d)):
        cfg_path = os.path.join(d, x, "db_config.json")
        if not os.path.isfile(cfg_path):
            continue
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                if json.load(fh).get("backend") == "psycopg":
                    out.append(x)
        except (OSError, ValueError):
            continue
    return out


def _db(env):
    if env not in _regions():
        abort(404)
    return DatabaseManager(os.path.join(_env_dir(), env, "db_config.json"))


@app.route("/")
def index():
    return render_template("index.html", regions=_regions(), runs=_registry()[:8])


@app.route("/mint", methods=["POST"])
def mint():
    label = re.sub(r"[^A-Za-z0-9]", "", request.form.get("label") or "cockpit")[:24] or "cockpit"
    p = subprocess.run(
        [sys.executable, os.path.join(REPO, "environmentscripts", "migration_manager.py"),
         "--label", label],
        capture_output=True, text=True, cwd=REPO, timeout=600)
    m = re.search(r"Test environment ready:\s*(\S+)", p.stdout)
    if p.returncode != 0 or not m:
        return render_template("error.html",
                               title="Minting failed",
                               detail=(p.stdout or "")[-1200:] + (p.stderr or "")[-800:])
    return redirect(url_for("index"))


# ---------------------------------------------------------------- section 1: towns

@app.route("/region/<env>/towns", methods=["GET", "POST"])
def town_knobs(env):
    db = _db(env)
    if request.method == "POST":
        # For each town x knob: blank or == region-wide default -> clear the override (fall back);
        # otherwise upsert. Delete-then-insert keeps it simple and idempotent.
        for cat, name, _label, _unit, _desc in TOWN_KNOBS:
            region_wide = _region_default(db, cat, name)
            for town in request.form.getlist("town"):
                val = (request.form.get(f"{town}::{cat}.{name}") or "").strip()
                # provenance is a claim the adopter makes — blank persists as NULL,
                # never as a placeholder string (lb-ba #9)
                src = (request.form.get(f"src::{town}") or "").strip() or None
                db.execute_non_query(
                    "DELETE FROM reference.TownParameterOverride WHERE Town=? AND Category=? AND Name=?",
                    (town, cat, name))
                if val and val != str(region_wide):
                    db.execute_non_query(
                        "INSERT INTO reference.TownParameterOverride (Town, Category, Name, Value, Source) "
                        "VALUES (?,?,?,?,?)", (town, cat, name, val, src))
        return redirect(url_for("town_knobs", env=env, saved=1))

    towns = [r[0] for r in db.execute_query(
        "SELECT DISTINCT Town FROM reference.Properties WHERE Town IS NOT NULL ORDER BY Town")]
    # A gold-minted universe carries no town labels (towns arrive with imported
    # region bundles) — the template says so instead of a silent empty table.
    defaults = {(c, n): _region_default(db, c, n) for c, n, _l, _u, _d in TOWN_KNOBS}
    overrides = {(t, c, n): v for t, c, n, v in db.execute_query(
        "SELECT Town, Category, Name, Value FROM reference.TownParameterOverride")}
    sources = {t: s for t, s in db.execute_query(
        "SELECT DISTINCT Town, Source FROM reference.TownParameterOverride "
        "WHERE Source IS NOT NULL")}
    return render_template("towns.html", env=env, towns=towns, knobs=TOWN_KNOBS,
                           defaults=defaults, overrides=overrides, sources=sources,
                           saved=request.args.get("saved"))


def _region_default(db, category, name):
    row = db.execute_query(
        "SELECT Value FROM reference.ParameterRegistryDefault WHERE Category=? AND Name=?",
        (category, name))
    return row[0][0] if row else ""


# ---------------------------------------------------------------- section 2: parameter sets

@app.route("/region/<env>/projections")
def projections(env):
    db = _db(env)
    rows = db.execute_query(
        "SELECT p.ProjectionID, p.Name, p.Description, p.Kind, p.ScenarioTag, "
        "       (SELECT COUNT(*) FROM reference.ParameterRegistryDefined d "
        "        WHERE d.ProjectionID = p.ProjectionID) "
        "FROM reference.Projection p ORDER BY p.ProjectionID")
    canonical = [r for r in rows if r[0] < CLONE_BASE]
    clones = [r for r in rows if r[0] >= CLONE_BASE]
    return render_template("projections.html", env=env, canonical=canonical, clones=clones,
                           clone_base=CLONE_BASE)


@app.route("/region/<env>/projections/clone", methods=["POST"])
def clone_projection(env):
    db = _db(env)
    src = int(request.form["source"])
    name = (request.form.get("name") or "").strip() or f"clone of {src}"
    if db.execute_query("SELECT 1 FROM reference.Projection WHERE Name = ?", (name,)):
        return render_template(
            "error.html", title="Name already taken",
            detail=f"A parameter set named {name!r} already exists — names are unique. "
                   f"Pick another."), 409
    row = db.execute_query(
        "SELECT MAX(ProjectionID) FROM reference.Projection WHERE ProjectionID >= ?", (CLONE_BASE,))
    new_id = (row[0][0] or CLONE_BASE - 1) + 1
    # Kind stays within the schema's vocabulary (ck_projection_kind); clones are
    # 'scenario' sets — the ID range (>= CLONE_BASE) is what marks them as cockpit-made.
    db.execute_non_query(
        "INSERT INTO reference.Projection (ProjectionID, Name, Description, Kind, ScenarioTag) "
        "SELECT ?, ?, ?, 'scenario', ScenarioTag FROM reference.Projection WHERE ProjectionID = ?",
        (new_id, name[:100], f"cockpit clone of projection {src}", src))
    db.execute_non_query(
        "INSERT INTO reference.ParameterRegistryDefined (ProjectionID, Category, Name, Value, DataType, Description) "
        "SELECT ?, Category, Name, Value, DataType, Description "
        "FROM reference.ParameterRegistryDefined WHERE ProjectionID = ?",
        (new_id, src))
    return redirect(url_for("edit_projection", env=env, pid=new_id))


@app.route("/region/<env>/projections/<int:pid>", methods=["GET", "POST"])
def edit_projection(env, pid):
    db = _db(env)
    proj = db.execute_query(
        "SELECT ProjectionID, Name, Description, Kind, ScenarioTag "
        "FROM reference.Projection WHERE ProjectionID = ?", (pid,))
    if not proj:
        abort(404)
    editable = pid >= CLONE_BASE

    if request.method == "POST":
        if not editable:
            # the protected-canonical rule, server-side — never trust the UI alone
            abort(403, "Canonical parameter sets are read-only; clone to experiment.")
        for key, raw in request.form.items():
            if "::" not in key or key.startswith("src::"):
                continue
            cat, name = key.split("::", 1)
            val = raw.strip()
            drow = db.execute_query(
                "SELECT Value, DataType FROM reference.ParameterRegistryDefault "
                "WHERE Category=? AND Name=?", (cat, name))
            if not drow:
                continue  # unknown key — never invent registry rows from form data
            default_val, dtype = str(drow[0][0]), drow[0][1]
            # the adopter's own provenance claim; blank persists as NULL, never a
            # placeholder — and never silently inherits prose describing a value
            # this override just replaced (lb-ba #9)
            src = (request.form.get(f"src::{cat}::{name}") or "").strip() or None
            db.execute_non_query(
                "DELETE FROM reference.ParameterRegistryDefined "
                "WHERE ProjectionID=? AND Category=? AND Name=?", (pid, cat, name))
            if val and val != default_val:
                db.execute_non_query(
                    "INSERT INTO reference.ParameterRegistryDefined "
                    "(ProjectionID, Category, Name, Value, DataType, Description) "
                    "VALUES (?,?,?,?,?,?)",
                    (pid, cat, name, val, dtype, src))
        return redirect(url_for("edit_projection", env=env, pid=pid, saved=1))

    cats = {c: (d, o) for c, d, o in db.execute_query(
        "SELECT Category, Description, DisplayOrder FROM reference.ParameterCategory")}
    defaults = db.execute_query(
        "SELECT Category, Name, Value, DataType, Description "
        "FROM reference.ParameterRegistryDefault ORDER BY Category, Name")
    overrides = {}
    for c, n, v, d in db.execute_query(
            "SELECT Category, Name, Value, Description FROM reference.ParameterRegistryDefined "
            "WHERE ProjectionID = ?", (pid,)):
        overrides[(c, n)] = (v, d)
    grouped = {}
    for c, n, v, dt, desc in defaults:
        ov = overrides.get((c, n))
        grouped.setdefault(c, []).append(
            (n, str(v), dt, desc or "", ov[0] if ov else None, ov[1] if ov else None))
    ordered = sorted(grouped.items(), key=lambda kv: cats.get(kv[0], ("", 999))[1])
    return render_template("projection_edit.html", env=env, proj=proj[0], editable=editable,
                           ordered=ordered, cats=cats, n_overrides=len(overrides),
                           saved=request.args.get("saved"))


# ---------------------------------------------------------------- run

def _registry():
    path = os.path.join(RUNS_DIR, "registry.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return sorted(json.load(fh), key=lambda r: r.get("started", ""), reverse=True)
    except (OSError, ValueError):
        return []


def _registry_write(rows):
    os.makedirs(RUNS_DIR, exist_ok=True)
    with open(os.path.join(RUNS_DIR, "registry.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1)


def _registry_update(run_id, **fields):
    with _REG_LOCK:
        rows = _registry()
        for r in rows:
            if r["id"] == run_id:
                r.update(fields)
        _registry_write(rows)


def _env_busy(env):
    """One run per environment at a time — concurrent sims on one database would
    race RunID assignment. (Batches serialize internally; this guards the door.)"""
    return any(r.get("env") == env and r.get("state") == "running" for r in _registry())


def _launch(env, pid, seed, months):
    """Start one simulation subprocess; returns the registry entry."""
    run_id = uuid.uuid4().hex[:10]
    os.makedirs(RUNS_DIR, exist_ok=True)
    log_path = os.path.join(RUNS_DIR, f"{run_id}.log")
    log = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "app", "src", "simulation.py"),
         "--env", env, "--projection-id", str(pid), "--seed", str(seed),
         "--months", str(months)],
        stdout=log, stderr=subprocess.STDOUT, cwd=REPO)
    entry = {"id": run_id, "pid": proc.pid, "env": env, "projection": pid,
             "seed": int(seed), "months": int(months), "state": "running",
             "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    with _REG_LOCK:
        rows = _registry()
        rows.append(entry)
        _registry_write(rows)

    def _wait():
        rc = proc.wait()
        log.close()
        _registry_update(run_id, state="completed" if rc == 0 else "failed", returncode=rc)
    threading.Thread(target=_wait, daemon=True).start()
    return entry


@app.route("/run", methods=["GET", "POST"])
def run_page(env=None):
    envs = _regions()
    if request.method == "POST":
        env = request.form["env"]
        if env not in envs:
            abort(400)
        if _env_busy(env):
            return render_template(
                "error.html", title="Environment busy",
                detail=f"{env} already has a run in progress — one run per environment "
                       f"at a time (concurrent runs would race). Mint another environment "
                       f"(~1 second) or wait for the current run."), 409
        entry = _launch(env, int(request.form["projection"]),
                        int(request.form.get("seed") or 12345),
                        int(request.form.get("months") or 240))
        return redirect(url_for("run_status", run_id=entry["id"]))
    sel = request.args.get("env") or (envs[0] if envs else None)
    projections_ = []
    if sel:
        projections_ = _db(sel).execute_query(
            "SELECT ProjectionID, Name, Kind FROM reference.Projection ORDER BY ProjectionID")
    return render_template("run.html", envs=envs, sel=sel, projections=projections_,
                           runs=_registry())


_DATE_LINE = re.compile(r"^\s*(\d{4})-(\d{2})-\d{2}:", re.M)


def _progress(entry):
    """(months_done, last_lines) parsed from the run's log. Degrades to the tail —
    never wrong, at worst less pretty."""
    log_path = os.path.join(RUNS_DIR, f"{entry['id']}.log")
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return 0, ""
    dates = _DATE_LINE.findall(text[-40000:])
    months = 0
    if dates:
        y, m = int(dates[-1][0]), int(dates[-1][1])
        months = max(0, (y - 2025) * 12 + m - 1)
    return min(months, entry["months"]), text[-2500:]


@app.route("/run/<run_id>")
def run_status(run_id):
    entry = next((r for r in _registry() if r["id"] == run_id), None)
    if not entry:
        abort(404)
    months, tail = _progress(entry)
    pct = int(100 * months / max(1, entry["months"]))
    return render_template("run_status.html", r=entry, months=months, pct=pct, tail=tail)


@app.route("/run/batch", methods=["POST"])
def run_batch():
    """Sequential N-seed batch — one worker on purpose: the cockpit teaches; the
    corpus tooling scales."""
    env = request.form["env"]
    if env not in _regions():
        abort(400)
    if _env_busy(env):
        return render_template(
            "error.html", title="Environment busy",
            detail=f"{env} already has a run in progress — one run per environment "
                   f"at a time. Mint another environment or wait."), 409
    pid = int(request.form["projection"])
    months = int(request.form.get("months") or 240)
    first = int(request.form.get("seed_first") or 1)
    n = max(1, min(50, int(request.form.get("seed_count") or 5)))
    batch_id = uuid.uuid4().hex[:10]
    entry = {"id": batch_id, "kind": "batch", "env": env, "projection": pid,
             "seed": f"{first}-{first + n - 1}", "months": months, "state": "running",
             "started": time.strftime("%Y-%m-%d %H:%M:%S"), "members": [], "done": 0, "total": n}
    with _REG_LOCK:
        rows = _registry()
        rows.append(entry)
        _registry_write(rows)

    def _worker():
        members = []
        for seed in range(first, first + n):
            sub = _launch(env, pid, seed, months)
            members.append(sub["id"])
            _registry_update(batch_id, members=members)
            while True:
                e = next((x for x in _registry() if x["id"] == sub["id"]), None)
                if e and e.get("state") != "running":
                    break
                time.sleep(2)
            done = len(members)
            _registry_update(batch_id, done=done)
            b = next((x for x in _registry() if x["id"] == batch_id), None)
            if b and b.get("state") == "cancelled":
                return
        _registry_update(batch_id, state="completed")
    threading.Thread(target=_worker, daemon=True).start()
    return redirect(url_for("batch_status", batch_id=batch_id))


@app.route("/batch/<batch_id>")
def batch_status(batch_id):
    b = next((r for r in _registry() if r["id"] == batch_id), None)
    if not b or b.get("kind") != "batch":
        abort(404)
    reg = {r["id"]: r for r in _registry()}
    members = [reg[m] for m in b.get("members", []) if m in reg]
    results_ = []
    survived = 0
    if members:
        db = _db(b["env"])
        for m in members:
            if m.get("state") != "completed":
                results_.append((m["seed"], m.get("state"), None, None, m["id"]))
                continue
            row = db.execute_query(
                "SELECT Status FROM simulation.Run WHERE ProjectionID=? AND RandomSeed=? "
                "ORDER BY RunID DESC", (b["projection"], m["seed"]))
            status = row[0][0] if row else "?"
            ok = status == "COMPLETED"
            survived += 1 if ok else 0
            results_.append((m["seed"], "survived" if ok else status, ok, None, m["id"]))
    return render_template("batch.html", b=b, results=results_, survived=survived,
                           finished=sum(1 for x in results_ if x[2] is not None))


@app.route("/run/<run_id>/cancel", methods=["POST"])
def run_cancel(run_id):
    entry = next((r for r in _registry() if r["id"] == run_id), None)
    if entry and entry.get("state") == "running":
        try:
            subprocess.run(["taskkill", "/PID", str(entry["pid"]), "/T", "/F"],
                           capture_output=True)
        except OSError:
            pass
        _registry_update(run_id, state="cancelled")
    return redirect(url_for("run_status", run_id=run_id))


# ---------------------------------------------------------------- results

@app.route("/run/<run_id>/results")
def results(run_id):
    entry = next((r for r in _registry() if r["id"] == run_id), None)
    if not entry:
        abort(404)
    if entry.get("state") != "completed":
        return redirect(url_for("run_status", run_id=run_id))
    db = _db(entry["env"])
    run_row = db.execute_query(
        "SELECT RunID, Status, StartDate, EndDate FROM simulation.Run "
        "WHERE ProjectionID=? AND RandomSeed=? ORDER BY RunID DESC",
        (entry["projection"], entry["seed"]))
    if not run_row:
        return render_template("error.html", title="No run found",
                               detail="The environment holds no run for this projection/seed.")
    run_id_db, status = run_row[0][0], run_row[0][1]

    ledger = db.execute_query(
        "SELECT LedgerDate, CashBalance, CSFBalance FROM simulation.FundLedger "
        "WHERE RunID=? ORDER BY EventID", (run_id_db,))
    series = []           # month-end (date, cash, csf)
    for d, cash, csf in ledger:
        key = (d.year, d.month)
        if series and series[-1][0] == key:
            series[-1] = (key, d, float(cash), float(csf))
        else:
            series.append((key, d, float(cash), float(csf)))
    series = [(d, c, f) for _k, d, c, f in series]

    final_cash = series[-1][1] if series else 0.0
    final_csf = series[-1][2] if series else 0.0
    evictions = db.execute_query(
        "SELECT COUNT(*) FROM simulation.LeaseTermination "
        "WHERE RunID=? AND TerminationType=?", (run_id_db, "EVICTION"))[0][0]
    leases = db.execute_query(
        "SELECT COUNT(*) FROM simulation.Lease WHERE RunID=?", (run_id_db,))[0][0]
    props = db.execute_query(
        "SELECT COUNT(*) FROM simulation.Properties WHERE RunID=?", (run_id_db,))[0][0]

    # The assumptions behind this run: the parameter set's overrides are keyed to the
    # ProjectionID the run used; town overrides are loose environment state with no
    # run-time stamp, so they are labeled as CURRENT configuration, never as run
    # provenance (lb-ba #9 — never print a claim the schema can't back).
    assumptions = db.execute_query(
        "SELECT d.Category, d.Name, d.Value, d.Description, r.Value "
        "FROM reference.ParameterRegistryDefined d "
        "LEFT JOIN reference.ParameterRegistryDefault r "
        "  ON r.Category = d.Category AND r.Name = d.Name "
        "WHERE d.ProjectionID = ? ORDER BY d.Category, d.Name", (entry["projection"],))
    town_override_count = db.execute_query(
        "SELECT COUNT(*) FROM reference.TownParameterOverride")[0][0]

    svg = _series_svg(series) if series else ""
    return render_template("results.html", r=entry, status=status,
                           final_cash=final_cash, final_csf=final_csf,
                           evictions=int(evictions), leases=int(leases), props=int(props),
                           svg=svg, n_months=len(series),
                           assumptions=assumptions,
                           town_override_count=int(town_override_count))


def _series_svg(series, w=640, h=260):
    """Cash + CSF monthly series as plain inline SVG — the site's idiom, no libraries."""
    left, right, top, bot = 56, 16, 14, 30
    xs = len(series)
    vmax = max(max(c for _d, c, _f in series), max(f for _d, _c, f in series), 1.0)
    def x(i):
        return left + (w - left - right) * (i / max(1, xs - 1))
    def y(v):
        return top + (h - top - bot) * (1 - v / vmax)
    def pts(vals):
        return " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    grid = ""
    for frac in (0, 0.5, 1.0):
        gy = y(vmax * frac)
        grid += (f'<line x1="{left}" y1="{gy:.1f}" x2="{w-right}" y2="{gy:.1f}" '
                 f'stroke="#E4DECF" stroke-width="1"/>'
                 f'<text x="{left-8}" y="{gy+4:.1f}" text-anchor="end" font-size="11" '
                 f'fill="#4A4638">${vmax*frac/1e6:.1f}M</text>')
    cash = f'<polyline fill="none" stroke="#A96A0C" stroke-width="2" points="{pts([c for _d, c, _f in series])}"/>'
    csf = f'<polyline fill="none" stroke="#3E7A5E" stroke-width="2" points="{pts([f for _d, _c, f in series])}"/>'
    x0, x1 = series[0][0], series[-1][0]
    labels = (f'<text x="{left}" y="{h-8}" font-size="11" fill="#4A4638">{x0:%Y-%m}</text>'
              f'<text x="{w-right}" y="{h-8}" text-anchor="end" font-size="11" '
              f'fill="#4A4638">{x1:%Y-%m}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Monthly Cash and CSF balances">'
            f'{grid}{cash}{csf}{labels}</svg>')


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
