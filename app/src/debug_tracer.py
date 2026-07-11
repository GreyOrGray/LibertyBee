"""
Debug tracer — captures every project function call and database operation.
Activated via --debug flag on simulation.py or master_test_runner.py.
Output: scratch/debug_logs/debug_<timestamp>.jsonl (one JSON object per line)

Entry types:
  {"t":"call","ts":"...","file":"app/src/fund_manager.py","func":"deposit_funds","line":42,"args":{...}}
  {"t":"db",  "ts":"...","method":"execute_non_query","sql":"INSERT INTO...","params":"(...)","rows":1}

Quick queries:
  grep '"func":"deposit_funds"' debug_*.jsonl
  python -c "import json,sys; [print(l) for l in open(sys.argv[1]) if json.loads(l)['t']=='db']" debug.jsonl
"""

import sys
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SKIP_FUNCS = frozenset({
    '__repr__', '__str__', '__hash__', '__eq__', '__ne__', '__lt__', '__le__',
    '__gt__', '__ge__', '__bool__', '__len__', '__iter__', '__next__',
    '__enter__', '__exit__', '__del__', '__get__', '__set__', '__delete__',
    'write', 'flush', 'close',
})


class DebugTracer:
    """
    Traces all project function calls via sys.settrace and all DB operations
    via explicit hooks in DatabaseManager. Zero modifications to other modules.

    Python guarantees the trace function itself is not traced recursively,
    so no re-entrancy guard is needed.
    """

    def __init__(self, output_path: Path, project_root: Path, buffer_size: int = 500):
        self.output_path = output_path
        # Lowercase for case-insensitive Windows path comparison
        self._root = str(project_root).lower().rstrip('/\\')
        self._buffer: list = []
        self._buffer_size = buffer_size
        self._file = None
        self._active = False
        self.call_count = 0
        self.db_count = 0

    def start(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.output_path, 'w', encoding='utf-8')
        self._active = True
        sys.settrace(self._trace)
        print(f"[debug] Tracing started -> {self.output_path}")

    def stop(self) -> None:
        self._active = False
        sys.settrace(None)
        self._flush()
        if self._file:
            self._file.close()
            self._file = None
        print(f"[debug] {self.call_count:,} function calls, {self.db_count:,} DB ops -> {self.output_path}")

    def log_db_call(self, method: str, sql: str, params: Any = None, rows: Any = None) -> None:
        """Called explicitly by DatabaseManager for every execute operation."""
        if not self._active:
            return
        self.db_count += 1
        self._write({
            't': 'db',
            'ts': _now(),
            'method': method,
            'sql': sql.strip()[:2000],
            'params': _safe_repr(params),
            'rows': rows,
        })

    def _trace(self, frame, event, arg):
        """
        Global trace function. sys.settrace only delivers 'call' events here.
        Returning None skips per-line local tracing for the frame — we only
        need entry points, not line-by-line execution.
        """
        if not self._active:
            return None
        filename = frame.f_code.co_filename
        lower = filename.lower()
        if not lower.startswith(self._root) or lower.endswith('debug_tracer.py'):
            return None
        func = frame.f_code.co_name
        if func in _SKIP_FUNCS:
            return None
        self.call_count += 1
        self._write({
            't': 'call',
            'ts': _now(),
            'file': _relpath(filename, self._root),
            'func': func,
            'line': frame.f_lineno,
            'args': _get_args(frame),
        })
        return None

    def _write(self, entry: dict) -> None:
        self._buffer.append(json.dumps(entry, default=str))
        if len(self._buffer) >= self._buffer_size:
            self._flush()

    def _flush(self) -> None:
        if self._file and self._buffer:
            self._file.write('\n'.join(self._buffer) + '\n')
            self._file.flush()
            self._buffer.clear()


# ---------------------------------------------------------------------------
# Helpers (module-level so they don't appear as project calls in the trace)
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relpath(filename: str, root: str) -> str:
    lower = filename.lower()
    rel = filename[len(root):].lstrip('/\\') if lower.startswith(root) else filename
    return rel.replace('\\', '/')


def _safe_repr(val: Any, max_len: int = 300) -> str:
    try:
        r = repr(val)
        return r if len(r) <= max_len else r[:max_len] + '...'
    except Exception:
        return '<unrepresentable>'


def _get_args(frame) -> dict:
    """Extract positional and keyword-only args from a call frame, excluding 'self'."""
    try:
        code = frame.f_code
        n = code.co_argcount + code.co_kwonlyargcount
        names = code.co_varnames[:n]
        locs = frame.f_locals
        return {
            name: _safe_repr(locs[name])
            for name in names
            if name != 'self' and name in locs
        }
    except Exception:
        return {}
