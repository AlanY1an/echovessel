"""Stage 1 add-ons · facts persistence + prompt boundary.

Three scenarios that earlier coverage did not pin:

- **1.4** ``timezone`` is stored on the persona row and survives
  ``GET /api/admin/persona``, but it MUST NOT leak into the rendered
  system prompt — the C-option contract only ships five facts to the
  model (full_name / gender / birth_year / occupation / native_language).
- **1.6** Renaming ``display_name`` via the admin API touches the
  ``personas`` row only; the five core_blocks are not rewritten and
  their ``last_edited_by`` / ``version`` columns stay put.
- **1.8** When the extraction route is called with ``existing_blocks``,
  the user prompt that hits the LLM contains those block contents
  verbatim — this is the contract that makes the "blank-write" path
  work (the LLM gets the user's own prose to reason over).
"""

from __future__ import annotations

import json
import tempfile
from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.sse import SSEBroadcaster
from echovessel.core.types import BlockLabel
from echovessel.memory import CoreBlock, Persona
from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
    load_config_from_str,
)
from echovessel.runtime.interaction import (
    PersonaFactsView,
    build_system_prompt,
)
from echovessel.runtime.llm import StubProvider


def _toml(data_dir: str) -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "stage1-test"
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


_VALID_LLM = json.dumps(
    {
        "core_blocks": {
            "persona_block": "你是温和的陪伴",
            "self_block": "",
            "user_block": "",
            "mood_block": "安静",
            "relationship_block": "",
        },
        "facts": {
            "full_name": "张丽华",
            "gender": "female",
            "birth_date": "1962-03-15",
            "ethnicity": None,
            "nationality": "CN",
            "native_language": "zh-CN",
            "locale_region": None,
            "education_level": None,
            "occupation": "retired_teacher",
            "occupation_field": None,
            "location": "沈阳",
            "timezone": "Asia/Shanghai",
            "relationship_status": "widowed",
            "life_stage": "retired",
            "health_status": "healthy",
        },
        "facts_confidence": 0.85,
    }
)


# ---------------------------------------------------------------------------
# Capture-LLM stub: records the user prompt so a test can assert on it.
# ---------------------------------------------------------------------------


class _CaptureLLM(StubProvider):
    """StubProvider variant that remembers the last (system, user) it saw.

    All other behaviour matches StubProvider — we only override
    ``complete`` so non-extraction call sites keep working.
    """

    def __init__(self, response: str) -> None:
        super().__init__(fallback=response)
        self.calls: list[tuple[str, str]] = []

    async def complete(  # type: ignore[override]
        self,
        system: str,
        user: str,
        **kwargs,
    ) -> tuple[str, object]:
        self.calls.append((system, user))
        return await super().complete(system, user, **kwargs)


def _build(llm_response: str = _VALID_LLM, *, llm: StubProvider | None = None):
    tmp = tempfile.mkdtemp(prefix="echovessel-stage1-")
    cfg = load_config_from_str(_toml(tmp))
    if llm is None:
        llm = StubProvider(fallback=llm_response)
    rt = Runtime.build(
        None,
        config_override=cfg,
        llm=llm,
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
    return rt, TestClient(app), llm


# ---------------------------------------------------------------------------
# 1.4 · timezone on the persona row, NOT in the prompt
# ---------------------------------------------------------------------------


def test_facts_timezone_persists_in_db_but_not_in_who_you_are_bullets() -> None:
    """``timezone`` rides the ``# Right now`` section via
    :func:`_format_now_section` rather than appearing as a bullet in
    ``# Who you are``. The fact columns that DO surface as bullets in
    v0.4 are name / gender / birth year / nationality / location /
    occupation / native language (plan §6.5).
    """

    rt, client, _llm = _build()
    with client:
        client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "P",
                "self_block": "",
                "user_block": "",
                "facts": {
                    "full_name": "张丽华",
                    "gender": "female",
                    "birth_date": "1962-03-15",
                    "occupation": "retired_teacher",
                    "native_language": "zh-CN",
                    "timezone": "Asia/Shanghai",
                    "location": "沈阳",
                },
            },
        )
        resp = client.get("/api/admin/persona")

    facts = resp.json()["facts"]
    assert facts["timezone"] == "Asia/Shanghai"
    assert facts["location"] == "沈阳"

    with DbSession(rt.ctx.engine) as db:
        persona_row = db.get(Persona, "stage1-test")
    view = PersonaFactsView.from_persona_row(persona_row)
    rendered = build_system_prompt(
        persona_display_name="Luna",
        core_blocks=[],
        persona_facts=view,
    )

    # Without ``now=``, the ``# Right now`` section is not rendered, so
    # the timezone string must not appear as a ``# Who you are`` bullet.
    assert "Asia/Shanghai" not in rendered
    # Location DOES render as a bullet (plan §6.5).
    assert "沈阳" in rendered
    assert "张丽华" in rendered
    assert "Born: 1962" in rendered
    assert "retired_teacher" in rendered
    assert "zh-CN" in rendered


# ---------------------------------------------------------------------------
# 1.6 · display_name rename does not touch core_blocks
# ---------------------------------------------------------------------------


def test_display_name_change_does_not_rewrite_core_blocks() -> None:
    """Changing the persona's display_name from the admin API edits the
    persona row only. Each core block carries its own ``last_edited_by``
    / ``version`` audit fields — the rename path must not touch them.
    """

    rt, client, _llm = _build()
    with client:
        client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "She is calm.",
                "self_block": "",
                "user_block": "She lives in 沈阳.",
                "mood_block": "",
            },
        )

        with DbSession(rt.ctx.engine) as db:
            blocks_before = list(
                db.exec(
                    select(CoreBlock).where(CoreBlock.persona_id == "stage1-test")
                )
            )
            snapshot_before = {
                getattr(b.label, "value", b.label): (
                    b.content,
                    b.version,
                    b.last_edited_by,
                )
                for b in blocks_before
            }

        # Rename via the partial-update endpoint.
        resp = client.post(
            "/api/admin/persona", json={"display_name": "Mochi"}
        )
        assert resp.status_code == 200

        with DbSession(rt.ctx.engine) as db:
            persona_row = db.get(Persona, "stage1-test")
            assert persona_row is not None
            assert persona_row.display_name == "Mochi"

            blocks_after = list(
                db.exec(
                    select(CoreBlock).where(CoreBlock.persona_id == "stage1-test")
                )
            )
            snapshot_after = {
                getattr(b.label, "value", b.label): (
                    b.content,
                    b.version,
                    b.last_edited_by,
                )
                for b in blocks_after
            }

    assert snapshot_before == snapshot_after, (
        "renaming the persona must not rewrite any core_blocks row "
        "(content, version, or last_edited_by)"
    )
    # Core block labels written by onboarding are still all there.
    assert BlockLabel.PERSONA.value in snapshot_after
    assert BlockLabel.USER.value in snapshot_after


# ---------------------------------------------------------------------------
# 1.8 · extract-from-input passes existing_blocks into the LLM prompt
# ---------------------------------------------------------------------------


def test_extract_from_input_inlines_existing_blocks_into_llm_user_prompt() -> None:
    """The extraction route's ``existing_blocks`` parameter must reach
    the LLM as part of the user prompt. Without this, the LLM has no
    way to know what the user already wrote, and the blank-write path
    loses its primary input.
    """

    capturing = _CaptureLLM(response=_VALID_LLM)
    _rt, client, llm = _build(llm=capturing)
    with client:
        resp = client.post(
            "/api/admin/persona/extract-from-input",
            json={
                "input_type": "blank_write",
                "existing_blocks": {
                    "persona_block": "她是退休的中学语文老师 · 性格温和",
                    "user_block": "我是她的学生",
                },
                "user_input": "",
                "locale": "zh-CN",
            },
        )

    assert resp.status_code == 200
    assert isinstance(llm, _CaptureLLM)
    assert llm.calls, "LLM was not called"
    _system, user_prompt = llm.calls[-1]

    # Both block contents the route received in existing_blocks must
    # appear verbatim somewhere in the user prompt body the LLM saw.
    assert "她是退休的中学语文老师 · 性格温和" in user_prompt
    assert "我是她的学生" in user_prompt
    # Locale hint also surfaces — this is contract from
    # format_persona_facts_user_prompt.
    assert "zh-CN" in user_prompt


# ---------------------------------------------------------------------------
# 1.9 · empty PersonaFactsView is equivalent to no view (regression guard)
# ---------------------------------------------------------------------------


def test_empty_facts_view_renders_same_prompt_as_legacy_no_facts_path() -> None:
    """A persona row with all-null facts must produce the same system
    prompt as the pre-facts code path (no ``# Who you are`` section,
    no spurious blank section header). This guards the soft-launch
    contract — old personas keep behaving exactly the same.
    """

    legacy = build_system_prompt(persona_display_name="Anyone", core_blocks=[])
    with_empty_view = build_system_prompt(
        persona_display_name="Anyone",
        core_blocks=[],
        persona_facts=PersonaFactsView(
            full_name=None,
            gender=None,
            birth_date=None,
            occupation=None,
            native_language=None,
        ),
    )
    assert legacy == with_empty_view

    # And explicitly: the prompt does not even mention the section header.
    assert "# Who you are" not in legacy

    # Add a row with one fact and confirm the section appears.
    one_fact = build_system_prompt(
        persona_display_name="Anyone",
        core_blocks=[],
        persona_facts=PersonaFactsView(birth_date=date(2001, 1, 1)),
    )
    assert "# Who you are" in one_fact
    assert "Born: 2001" in one_fact
