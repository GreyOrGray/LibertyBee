"""
Master Test Runner - Comprehensive system validation with complete event logging

This script runs the complete testing workflow to validate all system components
with full event logging and audit trail generation. It provides complete visibility
into every function call across all modules.

Workflow:
1. Database cleanup (optional with --clean flag)
2. Component validation testing
3a. Integration testing (simulation.py with configurable parameters), OR
3b. Regression-suite gate (--regression): run the full tracked suite as a promotion gate
4. Event analysis and validation

Parameters:
- --clean: Clean database before running tests (default: False)
- --months: Simulation duration for integration test (default: 6)
- --projection-id: Projection scenario ID (default: 206, the $8M released baseline)
- --seed: Random seed for reproducibility (default: 12345)
- --iterations: Number of simulation runs (default: 1)
- --regression: Run the full tracked regression suite (sql/regression_tests/run_suite.py)
  as a promotion gate instead of the quick integration smoke. The suite populates its own
  validated baseline (--reg-projection / --reg-months / --reg-seed) and asserts every
  invariant; the runner exits non-zero on any failure. Pair with --clean for a clean-room run.
- --reg-projection / --reg-months / --reg-seed: regression-gate baseline (default 206 / 60 / 99)

Usage (from app directory):
    python src/master_test_runner.py --clean --months 6
    python src/master_test_runner.py --months 30 --iterations 5
    python src/master_test_runner.py --months 240 --projection-id 206

Usage (from root directory):
    python app/src/master_test_runner.py --clean --months 6

Promotion gate (the automated regression check):
    python app/src/master_test_runner.py --env <db> --clean --regression
"""

import logging
import sys
import os
import subprocess
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple, Optional, TextIO

# Ensure we're running from app directory for proper imports and file access
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))  # this script's dir (app/src)
APP_DIR = os.path.dirname(SCRIPT_DIR)  # app/
PROJECT_ROOT = os.path.dirname(APP_DIR)  # repo root (for scratch/debug_logs output)


class TeeOutput:
    """
    Redirect output to both console and file.

    This class acts as a "tee" - writing to both stdout and a log file
    simultaneously when --debug-log flag is enabled.
    """
    def __init__(self, file_path: str):
        self.terminal = sys.stdout
        self.log = open(file_path, 'w', encoding='utf-8')

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def close(self):
        self.log.close()


def print_error_box(message: str):
    """
    Print error message with visual separators for visibility.

    Makes errors stand out in console output so they don't get buried.
    """
    border = "=" * 80
    print(f"\n{border}")
    print(f"ERROR: {message}")
    print(f"{border}\n")



class MasterTestRunner:
    """Orchestrate comprehensive system testing with event logging"""

    def __init__(self, env: str, configfile: str, clean_db: bool = False, months: int = 6,
                 projection_id: int = 206, seed: int = 12345, iterations: int = 1, debug: bool = False,
                 regression: bool = False, reg_projection: int = 206, reg_months: int = 60,
                 reg_seed: int = 99):

        # Import our modules
        from database_manager import DatabaseManager
        from event_logger import EventLogger
        print(f"Config File: {configfile}")
        self.env = env
        self.configfile = configfile
        self.db = DatabaseManager(self.configfile)
        self.test_results = {}
        self.start_time = datetime.now()
        self.clean_db = clean_db
        self.months = months
        self.projection_id = projection_id
        self.seed = seed
        self.iterations = iterations
        self.debug = debug
        # Regression-suite gate (--regression): runs the full tracked suite as the promotion
        # gate. The suite owns its own validated baseline (reg_* params), so it is decoupled
        # from the quick integration-smoke params above.
        self.regression = regression
        self.reg_projection = reg_projection
        self.reg_months = reg_months
        self.reg_seed = reg_seed
        self.tracer = None

        if debug:
            from debug_tracer import DebugTracer
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            project_root = Path(PROJECT_ROOT)
            log_path = project_root / "scratch" / "debug_logs" / f"debug_runner_{env}_{timestamp}.jsonl"
            self.tracer = DebugTracer(output_path=log_path, project_root=project_root)
            self.db.set_debug_tracer(self.tracer)

        # Set up logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)

    def run_database_cleanup(self) -> bool:
        """Phase 1: Clean database state (optional)"""
        if not self.clean_db:
            print("="*80)
            print("PHASE 1: DATABASE CLEANUP")
            print("="*80)
            print("SKIPPED (use --clean flag to enable)")
            print()
            return True

        print("="*80)
        print("PHASE 1: DATABASE CLEANUP")
        print("="*80)

        try:
            # Run cleanup procedure
            print("Running cleanup procedure...")
            cleanup_query = "EXEC [dbo].[sp_CleanSimulationEngine] @Type = NULL"
            self.db.execute_non_query(cleanup_query)
            print("OK Cleanup procedure completed")

            # Verify clean state
            print("\nVerifying clean state...")
            runs_count = self.db.execute_scalar("SELECT COUNT(*) FROM simulation.Run")
            events_count = self.db.execute_scalar("SELECT COUNT(*) FROM simulation.Event")
            inflation_count = self.db.execute_scalar("SELECT COUNT(*) FROM simulation.InflationSchedule")

            print(f"  Runs: {runs_count}")
            print(f"  Events: {events_count}")
            print(f"  Inflation Records: {inflation_count}")

            if runs_count == 0 and events_count == 0 and inflation_count == 0:
                print("OK Database state is clean")
                return True
            else:
                print("ERROR Database state is not clean")
                return False

        except Exception as e:
            print(f"WARNING Database cleanup failed: {e}")
            print("Continuing with tests - component cleanup will remove test data")
            return True

    def run_integration_test(self) -> bool:
        """Phase 3: Integration testing with simulation.py"""
        print("\n" + "="*80)
        print("PHASE 3: INTEGRATION TESTING")
        print("="*80)

        print(f"Running end-to-end simulation...")
        print(f"  Parameters: --env {self.env} --months {self.months} --projection-id {self.projection_id} --seed {self.seed} --iterations {self.iterations}")

        try:
            # Build command with parameters
            cmd = [
                sys.executable,
                "src/simulation.py",
                "--env", str(self.env),
                "--months", str(self.months),
                "--projection-id", str(self.projection_id),
                "--seed", str(self.seed),
                "--iterations", str(self.iterations),
            ]

            if self.debug:
                cmd.append("--debug")

            result = subprocess.run(
                cmd,
                cwd=".",
                capture_output=True,
                text=True,
                timeout=300  # 5 minutes timeout
            )
            
            success = result.returncode == 0

            if success:
                print("OK Integration test passed")
                #if result.stdout:
                    #print(result.stdout, end="")   # show prints from simulation
                if result.stderr:
                    print(result.stderr, end="")   # show warnings/debug on stderr
            else:
                print("ERROR Integration test failed")
                #if result.stdout:
                    #print(result.stdout, end="")
                if result.stderr:
                    print("Error output:")
                    print(result.stderr, end="")
            return success

        except subprocess.TimeoutExpired:
            print("ERROR Integration test timed out")
            self.test_results["Integration Test"] = {'success': False, 'error': 'Timeout'}
            return False
        except Exception as e:
            print(f"ERROR Integration test error: {e}")
            self.test_results["Integration Test"] = {'success': False, 'error': str(e)}
            return False

    def analyze_events(self) -> Dict:
        """Phase 4: Event analysis and validation"""
        print("\n" + "="*80)
        print("PHASE 4: EVENT ANALYSIS AND VALIDATION")
        print("="*80)

        try:
            # Complete event audit trail (only Simulation runs, not ComponentTest)
            print("Analyzing event audit trail (Simulation runs only)...")
            events_query = """
            SELECT EventType + '/' + COALESCE(ActionType, 'N/A') as EventAction,
                   CausalTag, LEFT(Metadata, 50) as MetadataPreview, LoggedAt, e.RunID
            FROM simulation.Event e
            INNER JOIN simulation.Run r ON e.RunID = r.RunID
            WHERE r.RunType = 'Simulation'
            ORDER BY EventID
            """

            events = self.db.execute_query(events_query)
            print(f"OK Found {len(events)} simulation events")

            # Show events by run for complete visibility
            if events:
                print("\nEvents by simulation run:")
                runs_query = """
                SELECT r.RunID, COUNT(*) as EventCount
                FROM simulation.Event e
                INNER JOIN simulation.Run r ON e.RunID = r.RunID
                WHERE r.RunType = 'Simulation'
                GROUP BY r.RunID
                ORDER BY r.RunID
                """

                runs_with_events = self.db.execute_query(runs_query)
                for run_id, event_count in runs_with_events:
                    print(f"  Run {run_id}: {event_count} events")

                print("\nSample events (showing run context):")
                for i, event in enumerate(events[:8]):  # Show more events
                    print(f"  {i+1}. Run {event[4]}: {event[0]} | {event[1]} | {event[2]}...")
                if len(events) > 8:
                    print(f"  ... and {len(events) - 8} more events")

            # Event summary by module (Simulation runs only)
            print("\nEvent summary by module:")
            summary_query = """
            SELECT EventType, ActionType, CausalTag, COUNT(*) as Count
            FROM simulation.Event e
            INNER JOIN simulation.Run r ON e.RunID = r.RunID
            WHERE r.RunType = 'Simulation'
            GROUP BY EventType, ActionType, CausalTag
            ORDER BY Count DESC
            """

            summary = self.db.execute_query(summary_query)
            for row in summary:
                event_type, action_type, causal_tag, count = row
                action_str = f"/{action_type}" if action_type else ""
                module_str = f" ({causal_tag})" if causal_tag else ""
                print(f"  {event_type}{action_str}{module_str}: {count} events")

            # Show which modules logged events
            print("\nModule coverage analysis:")
            module_coverage_query = """
            SELECT DISTINCT CausalTag, COUNT(*) as EventCount
            FROM simulation.Event
            WHERE CausalTag IS NOT NULL
            GROUP BY CausalTag
            ORDER BY EventCount DESC
            """

            module_coverage = self.db.execute_query(module_coverage_query)
            if module_coverage:
                print("  Modules with events logged:")
                for causal_tag, event_count in module_coverage:
                    print(f"    {causal_tag}: {event_count} events")
            else:
                print("  WARNING: No module-specific events found")

            # Data persistence validation
            print("\nData persistence validation:")
            validation_query = """
            SELECT 'Runs' as TableName, COUNT(*) as RecordCount FROM simulation.Run
            UNION ALL
            SELECT 'Events', COUNT(*) FROM simulation.Event
            UNION ALL
            SELECT 'InflationSchedule', COUNT(*) FROM simulation.InflationSchedule
            """

            validation = self.db.execute_query(validation_query)
            for table_name, record_count in validation:
                print(f"  {table_name}: {record_count} records")

            # Check for errors
            error_query = "SELECT COUNT(*) FROM simulation.Event WHERE EventType = 'ERROR'"
            error_count = self.db.execute_scalar(error_query)

            if error_count > 0:
                print(f"\nWARNING Found {error_count} error events")
                error_details = self.db.execute_query(
                    "SELECT Metadata FROM simulation.Event WHERE EventType = 'ERROR'"
                )
                for error in error_details[:3]:  # Show first 3 errors
                    print(f"    {error[0]}")
            else:
                print("OK No error events found")

            return {
                'total_events': len(events),
                'event_summary': summary,
                'data_validation': validation,
                'error_count': error_count,
                'runs_with_events': runs_with_events if events else []
            }

        except Exception as e:
            print(f"ERROR Event analysis failed: {e}")
            return {'error': str(e)}

    def generate_summary_report(self, analysis_results: Dict):
        """Generate final summary report"""
        print("\n" + "="*80)
        print("MASTER TEST RUNNER - FINAL SUMMARY")
        print("="*80)

        end_time = datetime.now()
        duration = (end_time - self.start_time).total_seconds()

        print(f"Test run completed in {duration:.1f} seconds")
        print(f"Started: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Ended: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")

        print("\nModule Test Results:")
        all_passed = True
        for module_name, result in self.test_results.items():
            status = "OK PASS" if result.get('success', False) else "ERROR FAIL"
            print(f"  {module_name}: {status}")
            if not result.get('success', False):
                all_passed = False

        print(f"\nEvent Logging Summary:")
        if 'total_events' in analysis_results:
            print(f"  Total events logged: {analysis_results['total_events']}")
            print(f"  Error events: {analysis_results.get('error_count', 0)}")

            if 'runs_with_events' in analysis_results:
                runs_with_events = analysis_results['runs_with_events']
                print(f"  Runs with events: {len(runs_with_events)}")
                for run_id, event_count in runs_with_events:
                    print(f"    Run {run_id}: {event_count} events")

        print(f"\nOverall Result: {'OK ALL TESTS PASSED' if all_passed else 'ERROR SOME TESTS FAILED'}")

        if all_passed:
            print("\nSUCCESS Complete system validation successful!")
            print("   All modules working correctly with comprehensive event logging.")
            print("   Ready for production simulation runs.")
        else:
            print("\nWARNING System validation failed - check individual module results above.")

    def run_regression_suite(self) -> bool:
        """Phase 3b: Regression-suite gate (--regression).

        Runs the full tracked regression suite (sql/regression_tests/run_suite.py) with its
        validated baseline. The suite --populate's its own single baseline run (so it doubles
        as the integration test) and asserts every invariant; run_suite exits non-zero on any
        module failure. This is the automated promotion gate."""
        print("\n" + "="*80)
        print("PHASE 3b: REGRESSION SUITE (promotion gate)")
        print("="*80)

        run_suite = os.path.join(PROJECT_ROOT, "sql", "regression_tests", "run_suite.py")
        cmd = [
            sys.executable, run_suite,
            "--env", str(self.env),
            "--populate",
            "--projection-id", str(self.reg_projection),
            "--months", str(self.reg_months),
            "--seed", str(self.reg_seed),
        ]
        print(f"Running the tracked regression suite as the promotion gate...")
        print(f"  Baseline: projection {self.reg_projection} / {self.reg_months} months / seed {self.reg_seed}")

        try:
            result = subprocess.run(
                cmd,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=1800,  # 30 min — generous for a 60-mo populate + 24 modules
            )

            # Stream the suite's own per-module PASS/FAIL lines + summary for visibility
            if result.stdout:
                print(result.stdout, end="")
            if result.returncode != 0 and result.stderr:
                print("Error output:")
                print(result.stderr, end="")

            success = result.returncode == 0
            if success:
                print("\nOK Regression suite passed (all modules green)")
            else:
                print("\nERROR Regression suite FAILED — see module failures above")
            return success

        except subprocess.TimeoutExpired:
            print("ERROR Regression suite timed out")
            return False
        except Exception as e:
            print(f"ERROR Regression suite error: {e}")
            return False

    def run_complete_test_suite(self) -> bool:
        """Run the complete master test suite"""
        print("LIBERTY BEE SIMULATION - MASTER TEST RUNNER")
        print("Comprehensive system validation with event logging")
        print(f"Started: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")

        if self.tracer:
            self.tracer.start()

        try:
            # Phase 1: Database cleanup
            if not self.run_database_cleanup():
                print("ERROR Database cleanup failed - aborting test suite")
                return False

            if self.regression:
                # Phase 3b: Regression-suite gate. The suite --populate's its own validated
                # baseline run (which IS the integration sim), so we skip the quick smoke and
                # gate on the suite. Event analysis then reads that populated run for the trail.
                gate_success = self.run_regression_suite()
                self.test_results["Regression Suite"] = {'success': gate_success}
            else:
                # Phase 3: Integration test (quick smoke)
                gate_success = self.run_integration_test()
                self.test_results["Integration Test"] = {'success': gate_success}
                if not gate_success:
                    print("WARNING Integration test failed - continuing with event analysis")

            # Phase 4: Event analysis
            analysis_results = self.analyze_events()

            # Generate summary
            self.generate_summary_report(analysis_results)

            # Return overall success (the gate — integration or regression — must pass)
            return gate_success

        except Exception as e:
            print(f"ERROR Master test runner failed: {e}")
            return False
        finally:
            if self.tracer:
                self.tracer.stop()


def main():
    """Run the master test suite with argument parsing"""
    parser = argparse.ArgumentParser(
        description="Liberty Bee Master Test Runner - Comprehensive System Validation"
    )
    parser.add_argument(
        "--env",
        type=str,
        help="Environment to use"
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Clean database before running tests (default: False)"
    )
    parser.add_argument(
        "--months",
        type=int,
        default=6,
        help="Simulation duration for integration test in months (default: 6)"
    )
    parser.add_argument(
        "--projection-id",
        type=int,
        default=206,
        help="Projection scenario ID to use (default: 206, the $8M released baseline)"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=12345,
        help="Random seed for reproducibility (default: 12345)"
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="Number of simulation runs for integration test (default: 1)"
    )
    parser.add_argument(
        "--regression",
        action="store_true",
        help="Run the full tracked regression suite (sql/regression_tests/run_suite.py) as a "
             "promotion gate instead of the quick integration smoke. The suite populates its own "
             "validated baseline and asserts all invariants; exits non-zero on any failure. "
             "Pair with --clean for a clean-room run."
    )
    parser.add_argument(
        "--reg-projection",
        type=int,
        default=206,
        help="Regression-gate baseline projection id (default: 206)"
    )
    parser.add_argument(
        "--reg-months",
        type=int,
        default=60,
        help="Regression-gate baseline horizon in months (default: 60)"
    )
    parser.add_argument(
        "--reg-seed",
        type=int,
        default=99,
        help="Regression-gate baseline seed (default: 99)"
    )
    parser.add_argument(
        "--debug-log",
        action="store_true",
        help="Write all output to debug log file in environments/<env>/debug_<timestamp>.log"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Trace every project function call and DB operation to scratch/debug_logs/ (JSONL)"
    )

    args = parser.parse_args()

    if not args.env:
        print(f"No environment supplied")
        sys.exit()
    env_dir = os.path.dirname(APP_DIR) + "\\environments\\" + args.env  # Go up one level to app directory
    configfile = env_dir + "\\db_config.json"

    # Set up debug logging if requested
    tee_output = None
    if args.debug_log:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_log_path = os.path.join(env_dir, f"debug_{timestamp}.log")
        tee_output = TeeOutput(debug_log_path)
        sys.stdout = tee_output
        print(f"[debug] Debug logging enabled: {debug_log_path}")

    # Run from app/src — the single source of truth
    os.chdir(APP_DIR)
    sys.path.insert(0, str(APP_DIR))
    print(f"[app] using src dir {APP_DIR}")
    print(f"[app] using script dir {SCRIPT_DIR}")

    # Create runner with parameters
    runner = MasterTestRunner(
        env = args.env,
        configfile = configfile,
        clean_db=args.clean,
        months=args.months,
        projection_id=args.projection_id,
        seed=args.seed,
        iterations=args.iterations,
        debug=args.debug,
        regression=args.regression,
        reg_projection=args.reg_projection,
        reg_months=args.reg_months,
        reg_seed=args.reg_seed
    )
    success = runner.run_complete_test_suite()

    # Close debug log file if opened
    if tee_output:
        print(f"[debug] Debug log closed")  # Print before restoring stdout
        sys.stdout = tee_output.terminal  # Restore original stdout
        tee_output.close()

    # Exit with appropriate code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()