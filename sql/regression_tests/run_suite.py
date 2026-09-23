"""Liberty Bee regression-suite runner.

Discovers the Python regression-test modules under sql/regression_tests/ and runs
each with `--env <db> --assert`, collecting pass/fail. Optionally populates the DB
with a baseline simulation first (so the integration tests have a run to read).

Each test module is expected to expose a `--env <db> --assert` CLI that exits 0 on
all-pass / 1 on any failure (the Phase 3.x convention).

Usage:
    python sql/regression_tests/run_suite.py --env <db> [--populate] \
        [--projection-id 206] [--months 60] [--seed 99]
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys

SUITE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SUITE_DIR))  # sql/regression_tests -> sql -> repo
APP_SRC = os.path.join(REPO_ROOT, "app", "src")


def discover_tests() -> list[str]:
    mods = []
    for p in sorted(glob.glob(os.path.join(SUITE_DIR, "**", "*.py"), recursive=True)):
        name = os.path.basename(p)
        if name == "run_suite.py" or name.startswith("_") or "__pycache__" in p:
            continue
        # Only assertion test modules (named *_test_*). Non-test artifacts in the
        # tree — e.g. phase_3_7_11_tenant_pipeline_funnel.py, a diagnostic report
        # with no pass/fail — are intentionally excluded from the suite.
        if "_test_" not in name:
            continue
        mods.append(p)
    return mods


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, help="ephemeral DB name under environments/")
    ap.add_argument("--populate", action="store_true", help="run a baseline sim first")
    ap.add_argument("--projection-id", default="206")
    ap.add_argument("--months", default="60")
    ap.add_argument("--seed", default="99")
    args = ap.parse_args()

    if args.populate:
        print(f"[populate] sim proj {args.projection_id} {args.months}mo seed {args.seed} ...")
        r = subprocess.run(
            [sys.executable, os.path.join(APP_SRC, "simulation.py"),
             "--env", args.env, "--projection-id", str(args.projection_id),
             "--months", str(args.months), "--seed", str(args.seed)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print("[populate] FAILED:\n" + (r.stdout or "")[-1500:] + (r.stderr or "")[-1500:])
            sys.exit(2)
        print("[populate] done.\n")

    tests = discover_tests()
    print(f"Discovered {len(tests)} regression test module(s).\n")
    results = []
    for t in tests:
        rel = os.path.relpath(t, REPO_ROOT).replace(os.sep, "/")
        r = subprocess.run(
            [sys.executable, t, "--env", args.env, "--assert"],
            capture_output=True, text=True,
        )
        ok = r.returncode == 0
        out = (r.stdout or "") + (r.stderr or "")
        # capture the most informative line for the summary
        signal = ""
        for key in ("Result:", "Traceback", "Error", "Invalid object", "FAIL"):
            hits = [ln.strip() for ln in out.splitlines() if key in ln]
            if hits:
                signal = hits[-1]
                break
        results.append((rel, ok, signal))
        print(f"  [{'PASS' if ok else 'FAIL'}] {rel}\n         {signal[:110]}")

    npass = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {npass}/{len(results)} test modules passed ===")
    if npass != len(results):
        print("\nFailures:")
        for rel, ok, sig in results:
            if not ok:
                print(f"  - {rel}: {sig[:120]}")
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    main()
