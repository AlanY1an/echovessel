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


# Anchor for the eval test tree. modifyitems receives every collected
# item in the suite (pytest's `items` list is global, not scoped to the
# conftest's directory), so we filter by nodeid against this prefix
# before applying the skip marker — otherwise the marker leaks to the
# entire repo and silently skips every test.
_EVAL_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _EVAL_TESTS_DIR.parents[1]
_EVAL_NODEID_PREFIX = (
    _EVAL_TESTS_DIR.relative_to(_REPO_ROOT).as_posix() + "/"
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
        if item.nodeid.startswith(_EVAL_NODEID_PREFIX):
            item.add_marker(skip_eval)
