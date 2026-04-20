"""Unit tests for the Usage dataclass (introduced in #1 Stage 1)."""

from __future__ import annotations

import pytest

from echovessel.runtime.llm.usage import Usage


def test_usage_construction_with_all_fields():
    u = Usage(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=20,
        cache_creation_input_tokens=10,
    )
    assert u.input_tokens == 100
    assert u.output_tokens == 50
    assert u.cache_read_input_tokens == 20
    assert u.cache_creation_input_tokens == 10


def test_usage_cache_fields_default_to_zero():
    u = Usage(input_tokens=8, output_tokens=4)
    assert u.cache_read_input_tokens == 0
    assert u.cache_creation_input_tokens == 0


def test_usage_is_frozen():
    u = Usage(input_tokens=1, output_tokens=1)
    with pytest.raises((AttributeError, TypeError)):
        u.input_tokens = 99  # type: ignore[misc]


def test_usage_equality():
    a = Usage(input_tokens=10, output_tokens=5, cache_read_input_tokens=2)
    b = Usage(input_tokens=10, output_tokens=5, cache_read_input_tokens=2)
    c = Usage(input_tokens=10, output_tokens=5, cache_read_input_tokens=3)
    assert a == b
    assert a != c


def test_usage_slots_no_arbitrary_attributes():
    u = Usage(input_tokens=1, output_tokens=1)
    # frozen+slots raises AttributeError (or TypeError on some CPython 3.12 builds)
    with pytest.raises((AttributeError, TypeError)):
        u.nonexistent = "x"  # type: ignore[attr-defined]
