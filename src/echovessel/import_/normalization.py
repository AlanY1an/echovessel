"""File → UTF-8 plain text normalization.

The MVP only handles text-shaped formats (txt / md / json / csv / any
UTF-8 bytes). PDF / DOCX / HTML / audio are explicitly out of scope
(tracker §2.5, import spec §12). Binary or non-UTF-8 input raises
`NormalizationError` and the pipeline aborts with a permanent failure.

This module IS allowed to be format-aware (tracker §5 禁区 grep notes
that `normalization.py` can inspect file extensions). All format-
specific LLM routing still lives later in the pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from echovessel.import_.errors import NormalizationError


def normalize_bytes(raw: bytes, *, suffix: str = "") -> str:
    """Normalize raw bytes into plain UTF-8 text.

    Arguments:
        raw: The file contents. May also be a pasted-text bytes blob.
        suffix: Lower-cased file extension with the leading dot
            (``".txt"`` / ``".md"`` / ``".json"`` / ``".csv"`` / ``""``).

    Format handling:
        - ``.json``: parsed with :func:`json.loads`, then flattened
          into ``"key: value"`` lines so the LLM sees a human-readable
          representation rather than raw braces.
        - ``.md``: if a YAML-like front-matter block is present
          (``"---\\n...\\n---"``), it is merged into the body as
          ``"key: value"`` lines to keep metadata available to the LLM.
        - ``.csv``: returned as-is; chunking handles row batching.
        - everything else: decoded as UTF-8 without transformations.

    Raises:
        NormalizationError: bytes are not valid UTF-8, or (for ``.json``)
        the payload is not valid JSON.
    """
    try:
        # utf-8-sig is byte-identical to utf-8 for BOM-less input and
        # strips a leading U+FEFF (common in Windows-editor exports),
        # which would otherwise break json.loads and the `---` check.
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise NormalizationError(
            f"normalize_bytes: input is not valid UTF-8 (suffix={suffix!r}): {exc}"
        ) from exc

    suffix = suffix.lower()
    if suffix == ".json":
        return _flatten_json_text(text)
    if suffix == ".md":
        return _merge_frontmatter(text)
    # .txt / .csv / unknown / empty: identity.
    return text


def normalize_file(path: Path | str) -> str:
    """Read a file from disk and normalize it.

    Thin wrapper around :func:`normalize_bytes` — kept separate so
    tests that just want to exercise the decode logic can pass raw
    bytes without touching the filesystem.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise NormalizationError(
            f"normalize_file: cannot read {p}: {exc}"
        ) from exc
    return normalize_bytes(raw, suffix=p.suffix)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _flatten_json_text(text: str) -> str:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NormalizationError(
            f"normalize_bytes: invalid JSON: {exc}"
        ) from exc
    return _flatten_json_value(data)


def _flatten_json_value(value: Any, *, prefix: str = "") -> str:
    """Recursively flatten arbitrary JSON into "key: value" lines.

    For a list of dicts (common chat-log shape), each list element is
    rendered as its own block separated by a blank line so chunking
    can later split it naturally.
    """
    lines: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
            if isinstance(v, (dict, list)):
                nested = _flatten_json_value(v, prefix=key)
                if nested:
                    lines.append(nested)
            else:
                lines.append(f"{key}: {v}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            if isinstance(item, dict):
                block = _flatten_json_value(item, prefix="")
                if block:
                    lines.append(block)
                    lines.append("")  # blank-line separator → chunker breaks here
            else:
                lines.append(f"{prefix}[{i}]: {item}")
    else:
        lines.append(f"{prefix}: {value}" if prefix else str(value))
    return "\n".join(lines).strip() + "\n" if lines else ""


def _merge_frontmatter(text: str) -> str:
    """If `text` begins with a YAML-style ``---`` front-matter block,
    fold its ``key: value`` lines into the body so the LLM can see
    them. Non-front-matter inputs return unchanged.
    """
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 4)
    if end == -1:
        return text
    block = text[4:end]
    body_start = end + len("\n---")
    # Skip optional trailing newline after the closing fence.
    if body_start < len(text) and text[body_start] == "\n":
        body_start += 1
    body = text[body_start:]
    # Don't try to parse YAML — just treat each non-empty line as
    # pre-annotated "key: value" metadata and prepend.
    meta_lines = [ln for ln in block.splitlines() if ln.strip()]
    if not meta_lines:
        return body
    return "\n".join(meta_lines) + "\n\n" + body


__all__ = ["normalize_bytes", "normalize_file"]
