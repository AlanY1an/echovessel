"""D4 guard: runtime code never passes channel_id= to memory retrieval.

The D4 铁律 says that `memory.retrieve`, `memory.load_core_blocks`, and
`memory.list_recall_messages` must be called WITHOUT any channel_id kwarg.
This test scans every `.py` file in `src/echovessel/runtime/` for forbidden
patterns.

Regression check: if you add a new runtime file that calls memory retrieval,
this test will still catch violations.
"""

from __future__ import annotations

import re
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parents[2] / "src" / "echovessel" / "runtime"

# Callable names we audit.
AUDITED_CALLS = ("retrieve", "load_core_blocks", "list_recall_messages")

# Pattern matching `<call>(...)` optionally spanning multiple lines where
# `channel_id=` appears before the matching close paren.
_PAREN_PATTERN = re.compile(
    r"(?P<name>" + "|".join(re.escape(c) for c in AUDITED_CALLS) + r")\s*\(",
)


def _find_call_bodies(source: str) -> list[tuple[str, str, int]]:
    """Return (call_name, body_text, line_number) for every audited call."""
    results: list[tuple[str, str, int]] = []
    for m in _PAREN_PATTERN.finditer(source):
        name = m.group("name")
        start = m.end()  # right after the "("
        depth = 1
        i = start
        while i < len(source) and depth > 0:
            ch = source[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        body = source[start : i - 1]
        line_no = source.count("\n", 0, m.start()) + 1
        results.append((name, body, line_no))
    return results


def test_no_runtime_caller_passes_channel_id_to_memory_retrieval():
    violations: list[str] = []
    assert RUNTIME_DIR.exists(), f"runtime dir missing: {RUNTIME_DIR}"

    for py in RUNTIME_DIR.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        # Skip this file itself and the interaction module's comments — we
        # grep for `channel_id=` inside the *argument list only*.
        source = py.read_text(encoding="utf-8")
        for name, body, line_no in _find_call_bodies(source):
            if "channel_id=" in body:
                violations.append(
                    f"{py.relative_to(RUNTIME_DIR.parents[2])}:{line_no} — "
                    f"{name}(...) passes channel_id="
                )

    assert not violations, (
        "D4 铁律 violated: runtime must never pass channel_id= to memory "
        "retrieval. Offenders:\n  " + "\n  ".join(violations)
    )
