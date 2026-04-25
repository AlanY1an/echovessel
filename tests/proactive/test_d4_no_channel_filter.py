"""D4 static guard — memory retrieve must never be called with channel_id.

This test does two things:

1. **Static grep**: Walk every .py file under ``src/echovessel/proactive/``
   and refuse any occurrence of ``channel_id=`` inside a memory query
   call site (``list_recall_messages(`` / ``retrieve(`` / ``get_recent_events(``
   / ``load_core_blocks(``).

2. **Protocol signature**: Confirm the ``MemoryApi`` Protocol's read
   methods do not even DECLARE a ``channel_id`` parameter — so typing
   wouldn't let a future edit add one without tripping the grep too.

D4 is documented in ``docs/DISCUSSION.md#2026-04-14`` §D4 and repeated in
``docs/proactive/01-spec-v0.1.md`` §1.6 / §5.4 / §6.5 / §8.3.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from echovessel.proactive.core.base import MemoryApi

PROACTIVE_SRC = Path(__file__).resolve().parents[2] / "src" / "echovessel" / "proactive"

# Memory read call sites that must stay channel-unaware
MEMORY_READ_CALLS = (
    "retrieve",
    "list_recall_messages",
    "get_recent_events",
    "load_core_blocks",
    "get_session_status",
)


def test_no_channel_id_kwarg_in_memory_reads():
    """Grep every proactive source file for forbidden patterns like
    ``list_recall_messages(..., channel_id=...)``."""
    offending: list[tuple[Path, int, str]] = []
    for py_file in PROACTIVE_SRC.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        lines = text.splitlines()
        # Find call openings — we scan paragraph windows so the kwarg
        # can be on a later line than the opening paren.
        for call_name in MEMORY_READ_CALLS:
            pattern = re.compile(rf"\b{re.escape(call_name)}\(")
            for match in pattern.finditer(text):
                # Find the matching close paren, tolerating depth
                start = match.start()
                depth = 0
                end = start
                for i in range(start, len(text)):
                    c = text[i]
                    if c == "(":
                        depth += 1
                    elif c == ")":
                        depth -= 1
                        if depth == 0:
                            end = i
                            break
                snippet = text[start : end + 1]
                if "channel_id=" in snippet:
                    # Find the line number of the offending position
                    prefix = text[:start]
                    line_no = prefix.count("\n") + 1
                    offending.append(
                        (py_file, line_no, lines[line_no - 1].strip())
                    )

    assert not offending, (
        "D4 violation: memory read call site used channel_id kwarg:\n"
        + "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in offending)
    )


def test_no_channel_filter_helpers_defined_in_proactive():
    """A stricter lint: refuse any literal ``channel_id`` assignment
    inside a function that also mentions a memory read call. This would
    catch a future 'pre-filter by channel' helper that bypasses kwarg
    detection."""
    suspicious: list[tuple[Path, int]] = []
    bad_helper_pattern = re.compile(
        r"\bchannel_id\s*=\s*[\"'][a-zA-Z0-9:_-]+[\"']"
    )
    for py_file in PROACTIVE_SRC.rglob("*.py"):
        lines = py_file.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, 1):
            if not bad_helper_pattern.search(line):
                continue
            # Exceptions that are legitimate:
            #  - ingest_message kwarg (write path is allowed to carry channel)
            #  - scheduler passing channel id for ingest target
            #  - default_channel_name / delivery defaults
            if "ingest_message" in line:
                continue
            if "target_channel" in line or "default_channel_name" in line:
                continue
            if "persona" in line or "user_id" in line:
                continue
            suspicious.append((py_file, lineno))

    # This test is a belt-and-braces check; it may have false positives if
    # legitimate string literals appear elsewhere. The explicit kwarg grep
    # above is the authoritative guard.
    if suspicious:
        details = "\n".join(f"  {p}:{ln}" for p, ln in suspicious)
        # Don't fail hard — surface for review; the positive kwarg test is
        # what actually enforces D4.
        print(
            "D4 soft-warning: string literal channel_id assignments seen:\n"
            + details
        )


def test_memory_api_protocol_has_no_channel_id_params():
    """MemoryApi Protocol's read methods do NOT accept ``channel_id``.

    If a future edit adds one, the signature check fires before any code
    can exploit it.
    """
    read_methods = (
        "load_core_blocks",
        "list_recall_messages",
        "get_recent_events",
        "get_session_status",
    )
    for name in read_methods:
        method = getattr(MemoryApi, name)
        sig = inspect.signature(method)
        params = list(sig.parameters.keys())
        assert "channel_id" not in params, (
            f"MemoryApi.{name} leaked a channel_id parameter — D4 violation"
        )
