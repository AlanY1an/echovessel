"""Eval-suite fixtures + private-corpus gating.

The evaluation harness reads a private corpus at
``develop-docs/memory/05-eval-corpus-v0.1.yaml``. That tree is
gitignored (design-note tracker, not shipped to public clones), so
the entire ``tests/eval/`` suite must skip itself on hosts where the
corpus is unavailable — otherwise CI fails the moment a PR comes
from a fork or a fresh clone.

``pytest_collection_modifyitems`` applies a blanket skip marker to
every item collected under this directory when the corpus file is
missing. Contributors who have the corpus on disk see the tests run
normally.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_CORPUS_PATH = Path(__file__).resolve().parents[2] / (
    "develop-docs/memory/05-eval-corpus-v0.1.yaml"
)


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    if _CORPUS_PATH.is_file():
        return
    skip_eval = pytest.mark.skip(
        reason=(
            "eval corpus not available "
            "(develop-docs/memory/05-eval-corpus-v0.1.yaml is gitignored "
            "and not shipped to public clones)"
        )
    )
    for item in items:
        item.add_marker(skip_eval)
