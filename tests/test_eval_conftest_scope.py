"""Regression test for tests/eval/conftest.py scope.

The eval suite skips itself when its private corpus file is missing.
Pytest's ``modifyitems`` hook receives the global list of collected
items, so the conftest must filter by nodeid prefix before applying
the skip marker — otherwise it leaks to every test in the repo and
silently skips the entire suite.

This test invokes pytest in a subprocess on a known non-eval test and
asserts it actually runs (collected and not skipped). When the bug is
present, the same invocation reports the test as skipped with the
eval-corpus reason.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CORPUS_PATH = _REPO_ROOT / "develop-docs/memory/05-eval-corpus-v0.1.yaml"


def test_non_eval_tests_run_when_corpus_missing() -> None:
    if _CORPUS_PATH.is_file():
        # The conftest's hook is a no-op when the corpus exists; the
        # leakage path can't trigger so there is nothing to verify here.
        return

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/memory/test_consolidate.py::test_is_trivial_short_session_without_emotion",
            "-v",
            "-rs",
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        f"pytest invocation failed:\n{combined}"
    )
    assert "PASSED" in proc.stdout, (
        "non-eval test did not pass — the eval conftest's skip marker is "
        "leaking beyond tests/eval/.\n"
        f"stdout:\n{proc.stdout}"
    )
    assert "eval corpus not available" not in proc.stdout, (
        "non-eval test was tagged with the eval-corpus skip reason — "
        "the conftest filter is not scoping by nodeid.\n"
        f"stdout:\n{proc.stdout}"
    )
