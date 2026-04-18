"""Tests for the persona-facts admin routes (initiative 2026-04-persona-facts).

Covers three code paths:

- ``POST /api/admin/persona/onboarding`` now accepts a ``facts`` field.
- ``PATCH /api/admin/persona/facts`` for partial updates after onboarding.
- ``POST /api/admin/persona/extract-from-input`` (blank_write mode) —
  uses a stub LLM so the test runs offline. The import_upload path is
  harder to cover without a real import stack and is exercised by the
  existing bootstrap integration test.
"""

from __future__ import annotations

import json
import tempfile

from fastapi.testclient import TestClient
from sqlmodel import Session as DbSession

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.sse import SSEBroadcaster
from echovessel.memory import Persona
from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
    load_config_from_str,
)
from echovessel.runtime.llm import StubProvider


def _toml(data_dir: str) -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "facts-test"
display_name = "Initial"

[memory]
db_path = "memory.db"

[llm]
provider = "stub"
api_key_env = ""

[consolidate]
worker_poll_seconds = 1
worker_max_retries = 1

[idle_scanner]
interval_seconds = 60
"""


_VALID_LLM_RESPONSE = json.dumps(
    {
        "core_blocks": {
            "persona_block": "你是一位温和的陪伴",
            "self_block": "",
            "user_block": "用户住在沈阳",
            "mood_block": "安静、愿意倾听",
            "relationship_block": "",
        },
        "facts": {
            "full_name": "张丽华",
            "gender": "female",
            "birth_date": "1962-03-15",
            "ethnicity": None,
            "nationality": "CN",
            "native_language": "zh-CN",
            "locale_region": "northeast_china",
            "education_level": "bachelor",
            "occupation": "retired_teacher",
            "occupation_field": "literature",
            "location": "沈阳",
            "timezone": "Asia/Shanghai",
            "relationship_status": "widowed",
            "life_stage": "retired",
            "health_status": "healthy",
        },
        "facts_confidence": 0.85,
    }
)


def _build(llm_response: str = _VALID_LLM_RESPONSE) -> tuple[Runtime, TestClient]:
    tmp = tempfile.mkdtemp(prefix="echovessel-facts-")
    cfg = load_config_from_str(_toml(tmp))
    rt = Runtime.build(
        None,
        config_override=cfg,
        llm=StubProvider(fallback=llm_response),
        embed_fn=build_zero_embedder(),
    )
    broadcaster = SSEBroadcaster()
    channel = WebChannel(debounce_ms=50)
    channel.attach_broadcaster(broadcaster)
    app = build_web_app(
        channel=channel,
        broadcaster=broadcaster,
        runtime=rt,
        heartbeat_seconds=0.5,
    )
    return rt, TestClient(app)


# ---------------------------------------------------------------------------
# POST /api/admin/persona/onboarding now accepts `facts`
# ---------------------------------------------------------------------------


def test_onboarding_without_facts_leaves_columns_null() -> None:
    rt, client = _build()
    with client:
        resp = client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "P",
                "self_block": "",
                "user_block": "",
                "mood_block": "",
            },
        )
    assert resp.status_code == 200
    with DbSession(rt.ctx.engine) as db:
        row = db.get(Persona, "facts-test")
    assert row is not None
    assert row.full_name is None
    assert row.gender is None
    assert row.birth_date is None


def test_onboarding_with_facts_writes_all_supplied_fields() -> None:
    rt, client = _build()
    with client:
        resp = client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "P",
                "self_block": "",
                "user_block": "",
                "mood_block": "",
                "facts": {
                    "full_name": "张丽华",
                    "gender": "female",
                    "birth_date": "1962-03-15",
                    "nationality": "CN",
                    "native_language": "zh-CN",
                    "timezone": "Asia/Shanghai",
                    "relationship_status": "widowed",
                    "life_stage": "retired",
                },
            },
        )
    assert resp.status_code == 200

    with DbSession(rt.ctx.engine) as db:
        row = db.get(Persona, "facts-test")
    assert row is not None
    assert row.full_name == "张丽华"
    assert row.gender == "female"
    assert row.birth_date.isoformat() == "1962-03-15"
    assert row.nationality == "CN"
    assert row.native_language == "zh-CN"
    assert row.timezone == "Asia/Shanghai"
    assert row.relationship_status == "widowed"
    assert row.life_stage == "retired"


def test_onboarding_rejects_out_of_enum_gender_gracefully() -> None:
    """Enum values outside the vocabulary become None (field_validator
    coerces rather than raising 422), so the caller can still finish."""

    rt, client = _build()
    with client:
        resp = client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "P",
                "self_block": "",
                "user_block": "",
                "mood_block": "",
                "facts": {"gender": "alien"},
            },
        )
    assert resp.status_code == 200
    with DbSession(rt.ctx.engine) as db:
        row = db.get(Persona, "facts-test")
    assert row is not None
    assert row.gender is None


def test_onboarding_rejects_bad_birth_date_with_422() -> None:
    _rt, client = _build()
    with client:
        resp = client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "P",
                "self_block": "",
                "user_block": "",
                "mood_block": "",
                "facts": {"birth_date": "sometime in 1962"},
            },
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/admin/persona returns facts
# ---------------------------------------------------------------------------


def test_get_persona_returns_facts_dict_with_all_15_keys() -> None:
    _rt, client = _build()
    with client:
        resp = client.get("/api/admin/persona")
    assert resp.status_code == 200
    facts = resp.json()["facts"]
    expected_keys = {
        "full_name", "gender", "birth_date", "ethnicity",
        "nationality", "native_language", "locale_region",
        "education_level", "occupation", "occupation_field",
        "location", "timezone", "relationship_status",
        "life_stage", "health_status",
    }
    assert set(facts) == expected_keys
    # A fresh daemon has no facts written yet.
    assert all(v is None for v in facts.values())


def test_get_persona_after_onboarding_serialises_birth_date_as_iso() -> None:
    _rt, client = _build()
    with client:
        client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "P",
                "self_block": "",
                "user_block": "",
                "mood_block": "",
                "facts": {"birth_date": "1962-03-15", "gender": "female"},
            },
        )
        resp = client.get("/api/admin/persona")

    assert resp.status_code == 200
    facts = resp.json()["facts"]
    assert facts["birth_date"] == "1962-03-15"
    assert facts["gender"] == "female"


# ---------------------------------------------------------------------------
# PATCH /api/admin/persona/facts
# ---------------------------------------------------------------------------


def test_patch_facts_updates_only_supplied_fields() -> None:
    rt, client = _build()
    with client:
        client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "P",
                "self_block": "",
                "user_block": "",
                "mood_block": "",
                "facts": {
                    "full_name": "original",
                    "gender": "female",
                    "timezone": "Asia/Shanghai",
                },
            },
        )
        resp = client.patch(
            "/api/admin/persona/facts",
            json={"facts": {"timezone": "America/Los_Angeles"}},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["facts"]["timezone"] == "America/Los_Angeles"
    # Fields not in the PATCH body are untouched.
    assert payload["facts"]["full_name"] == "original"
    assert payload["facts"]["gender"] == "female"

    with DbSession(rt.ctx.engine) as db:
        row = db.get(Persona, "facts-test")
    assert row is not None
    assert row.full_name == "original"
    assert row.timezone == "America/Los_Angeles"


def test_patch_facts_explicit_null_clears_field() -> None:
    rt, client = _build()
    with client:
        client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "P",
                "self_block": "",
                "user_block": "",
                "mood_block": "",
                "facts": {"full_name": "kept", "occupation": "teacher"},
            },
        )
        resp = client.patch(
            "/api/admin/persona/facts",
            json={"facts": {"occupation": None}},
        )

    assert resp.status_code == 200
    with DbSession(rt.ctx.engine) as db:
        row = db.get(Persona, "facts-test")
    assert row is not None
    assert row.full_name == "kept"
    assert row.occupation is None


def test_patch_facts_rejects_missing_facts_key() -> None:
    _rt, client = _build()
    with client:
        resp = client.patch(
            "/api/admin/persona/facts", json={"timezone": "UTC"}
        )
    assert resp.status_code == 400


def test_patch_facts_rejects_bad_birth_date() -> None:
    _rt, client = _build()
    with client:
        resp = client.patch(
            "/api/admin/persona/facts",
            json={"facts": {"birth_date": "last spring"}},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/admin/persona/extract-from-input — blank_write mode
# ---------------------------------------------------------------------------


def test_extract_blank_write_returns_blocks_and_facts() -> None:
    _rt, client = _build()
    with client:
        resp = client.post(
            "/api/admin/persona/extract-from-input",
            json={
                "input_type": "blank_write",
                "user_input": "她是 62 岁退休的中学语文老师,住在沈阳,老伴去世多年",
                "locale": "zh-CN",
                "persona_display_name": "妈",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["input_type"] == "blank_write"
    assert data["core_blocks"]["persona_block"].startswith("你是一位温和")
    assert data["facts"]["full_name"] == "张丽华"
    assert data["facts"]["gender"] == "female"
    assert data["facts"]["birth_date"] == "1962-03-15"
    assert data["facts_confidence"] == 0.85
    assert data["events"] == []
    assert data["thoughts"] == []
    assert data["pipeline_status"] is None


def test_extract_blank_write_with_existing_blocks_only_is_accepted() -> None:
    _rt, client = _build()
    with client:
        resp = client.post(
            "/api/admin/persona/extract-from-input",
            json={
                "input_type": "blank_write",
                "existing_blocks": {"persona_block": "某位长者"},
                "locale": "zh-CN",
            },
        )
    assert resp.status_code == 200


def test_extract_blank_write_without_any_input_is_400() -> None:
    _rt, client = _build()
    with client:
        resp = client.post(
            "/api/admin/persona/extract-from-input",
            json={"input_type": "blank_write"},
        )
    assert resp.status_code == 400
    assert "user_input or existing_blocks" in resp.json()["detail"]


def test_extract_refuses_after_onboarding_with_409() -> None:
    _rt, client = _build()
    with client:
        # Complete onboarding first.
        client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "already onboarded",
                "self_block": "",
                "user_block": "",
                "mood_block": "",
            },
        )
        resp = client.post(
            "/api/admin/persona/extract-from-input",
            json={
                "input_type": "blank_write",
                "user_input": "再来一次",
            },
        )
    assert resp.status_code == 409


def test_extract_bad_llm_json_returns_502() -> None:
    _rt, client = _build(llm_response="this is not JSON")
    with client:
        resp = client.post(
            "/api/admin/persona/extract-from-input",
            json={
                "input_type": "blank_write",
                "user_input": "some prose",
            },
        )
    assert resp.status_code == 502


def test_extract_rejects_unknown_input_type() -> None:
    _rt, client = _build()
    with client:
        resp = client.post(
            "/api/admin/persona/extract-from-input",
            json={"input_type": "nope", "user_input": "x"},
        )
    assert resp.status_code == 422


def test_extract_import_upload_without_importer_facade_is_503() -> None:
    _rt, client = _build()
    with client:
        resp = client.post(
            "/api/admin/persona/extract-from-input",
            json={
                "input_type": "import_upload",
                "upload_id": "u1",
            },
        )
    # The test runtime doesn't wire an importer_facade, so this path
    # should 503 rather than silently fall through.
    assert resp.status_code == 503
