"""Text → Chunks chunking per tracker §2.5.

Strategy summary:
  - Split on blank lines (``\\n\\s*\\n``) to find paragraph-ish segments.
  - Segments larger than ``MAX_CHUNK_CHARS`` (2000) are further sliced
    with a sliding window of ``WINDOW_CHARS`` (1500) / ``OVERLAP_CHARS``
    (500) so the LLM never sees more than a window at once but still
    gets context continuity between slices.
  - For CSV-looking text (lines of comma-separated values) we emit
    batches of up to ``CSV_BATCH`` lines per chunk so multiple rows
    share a single LLM call while still keeping the chunk under the
    size cap. The header line is repeated at the top of every batch so
    each independent LLM call sees the column names.

The chunker is intentionally format-ignorant after normalization — the
"is this csv" detection is a simple heuristic (every non-empty line
contains at least one comma AND no line has blank runs) and is the only
format awareness in the module.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

from echovessel.import_.models import Chunk

MAX_CHUNK_CHARS: int = 2000
WINDOW_CHARS: int = 1500
OVERLAP_CHARS: int = 500
CSV_BATCH: int = 8

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


def chunk_text(text: str, *, source_label: str = "") -> list[Chunk]:
    """Split ``text`` into a list of :class:`Chunk` instances.

    Always returns at least one chunk (possibly empty content) when the
    caller gave us a non-empty string. Whitespace-only input returns an
    empty list.
    """
    if not text or not text.strip():
        return []

    if _looks_like_csv(text):
        contents = list(_csv_chunks(text))
    else:
        paragraphs = _split_paragraphs(text)
        contents = list(_flatten_to_chunks(paragraphs))

    total = len(contents)
    chunks: list[Chunk] = []
    running_offset = 0
    for idx, body in enumerate(contents):
        chunks.append(
            Chunk(
                chunk_index=idx,
                total_chunks=total,
                content=body,
                offset=running_offset,
                source_label=source_label,
            )
        )
        running_offset += len(body)
    return chunks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_paragraphs(text: str) -> list[str]:
    parts = _PARAGRAPH_SPLIT.split(text.strip())
    return [p.strip() for p in parts if p.strip()]


def _flatten_to_chunks(paragraphs: Iterable[str]) -> Iterator[str]:
    """Convert paragraph list into chunk bodies.

    Short paragraphs are yielded one-for-one. Long paragraphs are
    sliced with the sliding window strategy.
    """
    for para in paragraphs:
        if len(para) <= MAX_CHUNK_CHARS:
            yield para
            continue
        yield from _sliding_window(para)


def _sliding_window(text: str) -> Iterator[str]:
    """1500 char window with 500 char overlap."""
    if not text:
        return
    start = 0
    length = len(text)
    stride = max(WINDOW_CHARS - OVERLAP_CHARS, 1)
    while start < length:
        end = min(start + WINDOW_CHARS, length)
        yield text[start:end]
        if end == length:
            return
        start += stride


def _looks_like_csv(text: str) -> bool:
    """Heuristic: every non-empty line has at least one comma, has at
    least 2 lines, and the median line is shorter than 400 chars.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    if not all("," in ln for ln in lines):
        return False
    median_len = sorted(len(ln) for ln in lines)[len(lines) // 2]
    return median_len < 400


def _csv_chunks(text: str) -> Iterator[str]:
    """Batch CSV rows so each batch fits the LLM call budget.

    Each chunk is an independent LLM call with no shared context, so
    every batch leads with the header line — otherwise batches after
    the first would present bare values with no column names. The
    header counts toward the ``CSV_BATCH`` line budget, leaving
    ``CSV_BATCH - 1`` data rows per chunk.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return
    header, rows = lines[0], lines[1:]
    if not rows:
        yield header
        return
    rows_per_batch = max(CSV_BATCH - 1, 1)
    for i in range(0, len(rows), rows_per_batch):
        yield "\n".join([header, *rows[i : i + rows_per_batch]])


__all__ = [
    "Chunk",
    "chunk_text",
    "MAX_CHUNK_CHARS",
    "WINDOW_CHARS",
    "OVERLAP_CHARS",
    "CSV_BATCH",
]
