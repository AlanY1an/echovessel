"""v0.5 API contract pins for ``POST /api/admin/persona*``.

Three invariants:

1. ``OnboardingRequest`` rejects ``self_block`` with 422 (extra='forbid').
2. ``OnboardingRequest`` rejects ``relationship_block`` with 422.
3. ``GET /api/admin/persona`` returns exactly three core-block keys.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session as DbSession

from echovessel.channels.web.routes.admin import build_admin_router
from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.models import Persona


class _Registry:
    def register(self, _ch: Any) -> None: ...
    def iter_channels(self):
        return iter([])


class _Cfg:
    class _Runtime:
        data_dir = "/tmp"

    runtime = _Runtime()


class _Persona:
    id = "admin-test"
    display_name = "admin-test"
    voice_enabled = False
    voice_id = None


class _Ctx:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.registry = _Registry()
        self.config = _Cfg()
        self.persona = _Persona()
        self.config_path = None


class _Runtime:
    def __init__(self, engine) -> None:
        self.ctx = _Ctx(engine)

    def _atomic_write_config_field(self, *args, **kwargs) -> None: ...


def _build_client() -> TestClient:
    tmp = Path(tempfile.mkdtemp())
    engine = create_engine(tmp / "admin_contract.db")
    create_all_tables(engine)
    with DbSession(engine) as db:
        db.add(Persona(id="admin-test", display_name="admin-test"))
        db.commit()
    rt = _Runtime(engine)
    app = FastAPI()
    app.include_router(build_admin_router(runtime=rt))
    return TestClient(app)


def test_onboarding_request_rejects_self_block_field():
    client = _build_client()
    with client:
        resp = client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "P",
                "user_block": "U",
                "self_block": "illegal",
            },
        )
    assert resp.status_code == 422


def test_onboarding_request_rejects_relationship_block_field():
    client = _build_client()
    with client:
        resp = client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "P",
                "user_block": "U",
                "relationship_block": "illegal",
            },
        )
    assert resp.status_code == 422


def test_persona_update_rejects_self_block_field():
    client = _build_client()
    with client:
        resp = client.post(
            "/api/admin/persona",
            json={"self_block": "illegal"},
        )
    assert resp.status_code == 422


def test_persona_update_rejects_relationship_block_field():
    client = _build_client()
    with client:
        resp = client.post(
            "/api/admin/persona",
            json={"relationship_block": "illegal"},
        )
    assert resp.status_code == 422


def test_get_persona_returns_three_blocks_only():
    client = _build_client()
    with client:
        resp = client.get("/api/admin/persona")
    assert resp.status_code == 200
    blocks = resp.json()["core_blocks"]
    assert set(blocks.keys()) == {"persona", "user", "style"}
