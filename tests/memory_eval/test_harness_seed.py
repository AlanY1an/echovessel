"""Unit tests for ``FixtureSeed`` parsing — verify ``load_fixture`` populates
the dataclass fields from a YAML doc without invoking the live LLM pipeline."""

from __future__ import annotations

from pathlib import Path

from tests.memory_eval.harness import load_fixture


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "seed.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_style_block_round_trips_through_load_fixture(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
fixture_id: t_style
version: scripted
seed:
  persona_block: 你是陪伴
  style_block: 不要哈哈开头
turns: []
invariants: {}
""",
    )
    fix = load_fixture(path)
    assert fix.seed.style_block == "不要哈哈开头"
    assert fix.seed.persona_block == "你是陪伴"


def test_style_block_defaults_to_empty(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
fixture_id: t_no_style
version: scripted
seed:
  persona_block: 陪伴
turns: []
invariants: {}
""",
    )
    fix = load_fixture(path)
    assert fix.seed.style_block == ""


def test_persona_timezone_round_trips_through_load_fixture(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
fixture_id: t_tz
version: scripted
seed:
  persona_block: 陪伴
  persona_timezone: Asia/Taipei
  persona_location: 台北
turns: []
invariants: {}
""",
    )
    fix = load_fixture(path)
    assert fix.seed.persona_timezone == "Asia/Taipei"
    assert fix.seed.persona_location == "台北"


def test_persona_facts_default_to_none(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
fixture_id: t_no_tz
version: scripted
seed:
  persona_block: 陪伴
turns: []
invariants: {}
""",
    )
    fix = load_fixture(path)
    assert fix.seed.persona_timezone is None
    assert fix.seed.persona_location is None


def test_seed_thoughts_round_trips_through_load_fixture(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
fixture_id: t_seed_thoughts
version: scripted
seed:
  persona_block: 陪伴
  seed_thoughts:
    - description: 用户最近在思考职业方向
      emotional_impact: -3
      emotion_tags: [anxious]
      relational_tags: [unresolved]
      created_at_offset_hours: -10
    - description: 用户对未来感到迷茫
      emotional_impact: -4
      created_at_offset_hours: -6
turns: []
invariants: {}
""",
    )
    fix = load_fixture(path)
    assert len(fix.seed.seed_thoughts) == 2
    first, second = fix.seed.seed_thoughts
    assert first.description == "用户最近在思考职业方向"
    assert first.emotional_impact == -3
    assert first.emotion_tags == ["anxious"]
    assert first.relational_tags == ["unresolved"]
    assert first.created_at_offset_hours == -10.0
    assert second.description == "用户对未来感到迷茫"
    assert second.emotional_impact == -4
    assert second.created_at_offset_hours == -6.0


def test_seed_thoughts_defaults_to_empty(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
fixture_id: t_no_seed_thoughts
version: scripted
seed:
  persona_block: 陪伴
turns: []
invariants: {}
""",
    )
    fix = load_fixture(path)
    assert fix.seed.seed_thoughts == []
