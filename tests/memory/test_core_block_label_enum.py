"""v0.5 invariant: ``BlockLabel`` is exactly {persona, user, style}.

The enum is the single source of truth for L1 labels. Any future
edit that re-adds ``self`` / ``relationship`` / ``mood`` must go
through a new initiative — this test exists so a casual revert is
blocked at CI.
"""

from __future__ import annotations

from echovessel.core.types import (
    PER_USER_BLOCK_LABELS,
    SHARED_BLOCK_LABELS,
    BlockLabel,
)


def test_block_label_has_exactly_three_values():
    assert [label.value for label in BlockLabel] == ["persona", "user", "style"]


def test_shared_block_labels_are_persona_and_style():
    assert frozenset(
        {BlockLabel.PERSONA, BlockLabel.STYLE}
    ) == SHARED_BLOCK_LABELS


def test_per_user_block_labels_are_just_user():
    assert frozenset({BlockLabel.USER}) == PER_USER_BLOCK_LABELS


def test_legacy_labels_are_gone():
    """Belt-and-braces — a reader should not even be able to reach
    BlockLabel.SELF / RELATIONSHIP / MOOD via getattr.
    """
    for legacy in ("SELF", "RELATIONSHIP", "MOOD"):
        assert not hasattr(BlockLabel, legacy), (
            f"BlockLabel.{legacy} must not exist in v0.5"
        )
