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


def test_episodic_state_initial_round_trips_through_load_fixture(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        """
fixture_id: t_episodic_initial
version: scripted
seed:
  persona_block: 陪伴
  episodic_state_initial:
    mood: somber
    energy: 3
turns: []
invariants: {}
""",
    )
    fix = load_fixture(path)
    assert fix.seed.episodic_state_initial == {"mood": "somber", "energy": 3}


def test_episodic_state_initial_defaults_to_none(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
fixture_id: t_no_episodic_initial
version: scripted
seed:
  persona_block: 陪伴
turns: []
invariants: {}
""",
    )
    fix = load_fixture(path)
    assert fix.seed.episodic_state_initial is None


def test_force_load_user_thoughts_round_trips_through_load_fixture(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        """
fixture_id: t_force_load
version: scripted
seed:
  persona_block: 陪伴
turns: []
retrieve:
  query: 任意查询
  top_k: 5
  force_load_user_thoughts: 2
invariants: {}
""",
    )
    fix = load_fixture(path)
    assert fix.retrieve is not None
    assert fix.retrieve.force_load_user_thoughts == 2


def test_force_load_user_thoughts_defaults_to_zero(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
fixture_id: t_no_force_load
version: scripted
seed:
  persona_block: 陪伴
turns: []
retrieve:
  query: 任意查询
  top_k: 5
invariants: {}
""",
    )
    fix = load_fixture(path)
    assert fix.retrieve is not None
    assert fix.retrieve.force_load_user_thoughts == 0


def test_seed_thought_filling_round_trips_through_load_fixture(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        """
fixture_id: t_seed_filling
version: scripted
seed:
  persona_block: 陪伴
  seed_thoughts:
    - description: 用户对宠物有很深的情感连结
      filling:
        - parent_description_contains: Mochi
turns: []
invariants: {}
""",
    )
    fix = load_fixture(path)
    assert len(fix.seed.seed_thoughts) == 1
    thought = fix.seed.seed_thoughts[0]
    assert len(thought.filling) == 1
    assert thought.filling[0].parent_description_contains == "Mochi"


def test_seed_thought_filling_defaults_to_empty(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
fixture_id: t_no_seed_filling
version: scripted
seed:
  persona_block: 陪伴
  seed_thoughts:
    - description: 任意想法
turns: []
invariants: {}
""",
    )
    fix = load_fixture(path)
    assert fix.seed.seed_thoughts[0].filling == []


def test_post_consolidate_actions_round_trips_through_load_fixture(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        """
fixture_id: t_post_actions
version: scripted
seed:
  persona_block: 陪伴
turns: []
post_consolidate_actions:
  - kind: delete
    target_description_contains: foo
    choice: ORPHAN
invariants: {}
""",
    )
    fix = load_fixture(path)
    assert len(fix.post_consolidate_actions) == 1
    assert fix.post_consolidate_actions[0].target_description_contains == "foo"
    assert fix.post_consolidate_actions[0].choice == "ORPHAN"


def test_post_consolidate_actions_defaults_to_empty(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
fixture_id: t_no_post_actions
version: scripted
seed:
  persona_block: 陪伴
turns: []
invariants: {}
""",
    )
    fix = load_fixture(path)
    assert fix.post_consolidate_actions == []
