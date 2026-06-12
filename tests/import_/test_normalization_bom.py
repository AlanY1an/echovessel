"""BOM-prefixed UTF-8 input is valid UTF-8 and must normalize cleanly."""

from __future__ import annotations

import codecs

from echovessel.import_.normalization import normalize_bytes

BOM = codecs.BOM_UTF8


def test_bom_json_parses():
    raw = BOM + b'{"speaker": "alan", "text": "hello"}'
    out = normalize_bytes(raw, suffix=".json")
    assert "speaker: alan" in out
    assert "text: hello" in out


def test_bom_md_frontmatter_merged():
    doc = "---\ntitle: Diary\n---\n\nBody line.\n"
    out = normalize_bytes(BOM + doc.encode(), suffix=".md")
    assert "title: Diary" in out
    assert "Body line." in out
    assert "---" not in out


def test_bom_plain_text_stripped():
    out = normalize_bytes(BOM + b"name,age\nalan,30\n", suffix=".csv")
    assert out.startswith("name,age")
    assert "﻿" not in out


def test_no_bom_unchanged():
    out = normalize_bytes(b"plain text", suffix=".txt")
    assert out == "plain text"
