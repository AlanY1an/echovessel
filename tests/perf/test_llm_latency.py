"""LLM latency benchmark — measures real-model response times through the full pipeline.

Requires a live API key. Skips automatically when OPENAI_API_KEY is not set.

Run:
    OPENAI_API_KEY="sk-..." uv run python -m pytest tests/perf/test_llm_latency.py -v -s

The -s flag is important: it lets the streaming progress print to stdout in real time.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — skipping real-LLM latency tests",
)


def _build_config_toml(data_dir: str, *, model: str = "gpt-4o-mini") -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "bench"
display_name = "Benchmark Persona"

[memory]
db_path = ":memory:"

[llm]
provider = "openai_compat"
api_key_env = "OPENAI_API_KEY"
model = "{model}"
max_tokens = 256
temperature = 0.7
timeout_seconds = 30

[consolidate]
worker_poll_seconds = 999
worker_max_retries = 1

[idle_scanner]
interval_seconds = 999

[proactive]
enabled = false

[voice]
enabled = false

[channels.web]
enabled = false
"""


def _seed_persona(engine):
    from sqlmodel import Session, select

    from echovessel.memory.models import CoreBlock

    with Session(engine) as db:
        existing = db.exec(
            select(CoreBlock).where(CoreBlock.persona_id == "bench")
        ).first()
        if not existing:
            db.add(
                CoreBlock(
                    persona_id="bench",
                    user_id=None,
                    label="persona",
                    content="You are a helpful, concise assistant. Reply in one sentence.",
                )
            )
            db.commit()


def _build_turn_context(rt):
    from sqlmodel import Session

    from echovessel.runtime.turn.coordinator import TurnContext

    db = Session(rt.ctx.engine)
    return db, TurnContext(
        persona_id="bench",
        persona_display_name="Benchmark Persona",
        db=db,
        backend=rt.ctx.backend,
        embed_fn=rt.ctx.embed_fn,
        retrieve_k=rt.ctx.config.memory.retrieve_k,
        recent_window_size=rt.ctx.config.memory.recent_window_size,
    )


def _make_turn(content: str, ref: str = "perf"):
    from echovessel.channels.base import IncomingMessage, IncomingTurn

    msg = IncomingMessage(
        channel_id="bench",
        user_id="self",
        content=content,
        received_at=datetime.now(),
        external_ref=ref,
    )
    return IncomingTurn.from_single_message(msg)


@pytest.fixture
async def runtime():
    from echovessel.runtime import Runtime, build_zero_embedder, load_config_from_str

    tmp = Path(tempfile.mkdtemp(prefix="echovessel-perf-"))
    cfg = load_config_from_str(_build_config_toml(str(tmp)))
    rt = Runtime.build(None, config_override=cfg, embed_fn=build_zero_embedder())
    _seed_persona(rt.ctx.engine)
    yield rt


async def test_single_turn_latency(runtime):
    """Single turn: TTFT + total time + tokens/sec."""
    from echovessel.runtime.turn.coordinator import assemble_turn

    rt = runtime
    db, ctx = _build_turn_context(rt)
    turn = _make_turn("What is 2 + 2? Reply in one word.")

    tokens: list[tuple[float, str]] = []
    t0 = time.perf_counter()
    ttft: float | None = None

    async def on_token(mid: int, delta: str) -> None:
        nonlocal ttft
        now = time.perf_counter()
        if ttft is None:
            ttft = now
        tokens.append((now - t0, delta))

    try:
        result = await assemble_turn(ctx, turn, rt.ctx.llm, on_token=on_token)
    finally:
        db.close()

    t_end = time.perf_counter()
    total = t_end - t0
    ttft_ms = ((ttft - t0) * 1000) if ttft else total * 1000
    tps = len(tokens) / total if total > 0 else 0

    print("\n" + "=" * 60)
    print("Single Turn Latency")
    print("=" * 60)
    print(f"  Prompt:  \"{turn.messages[0].content}\"")
    print(f"  Reply:   \"{result.reply[:100]}\"")
    print(f"  TTFT:    {ttft_ms:.0f} ms")
    print(f"  Total:   {total*1000:.0f} ms")
    print(f"  Tokens:  {len(tokens)}")
    print(f"  Speed:   {tps:.1f} tok/s")
    print("=" * 60)

    assert result.reply, "Reply should not be empty"
    assert len(tokens) > 0, "Should have received at least one token"
    assert total < 30, f"Took {total:.1f}s — exceeds 30s timeout"


async def test_multi_turn_latency(runtime):
    """3 consecutive turns: does context accumulation degrade latency?"""
    from echovessel.runtime.turn.coordinator import assemble_turn

    rt = runtime
    prompts = [
        "What is the capital of France? One word.",
        "What about Germany? One word.",
        "And Japan? One word.",
    ]

    print("\n" + "=" * 60)
    print("Multi-Turn Latency (3 turns)")
    print("=" * 60)

    for i, prompt in enumerate(prompts):
        db, ctx = _build_turn_context(rt)
        turn = _make_turn(prompt, ref=f"perf-multi-{i}")

        ttft: float | None = None
        count = 0
        t0 = time.perf_counter()

        async def on_token(mid: int, delta: str) -> None:
            nonlocal ttft, count
            if ttft is None:
                ttft = time.perf_counter()
            count += 1

        try:
            result = await assemble_turn(ctx, turn, rt.ctx.llm, on_token=on_token)
        finally:
            db.close()

        total = time.perf_counter() - t0
        ttft_ms = ((ttft - t0) * 1000) if ttft else total * 1000

        print(f"  Turn {i+1}: \"{prompt}\"")
        print(f"    Reply: \"{result.reply[:80]}\"")
        print(f"    TTFT:  {ttft_ms:.0f} ms | Total: {total*1000:.0f} ms | Tokens: {count}")

        assert result.reply
        assert total < 30
        await asyncio.sleep(0.1)

    print("=" * 60)


async def test_model_comparison(runtime):
    """gpt-4o-mini vs gpt-4o: compare TTFT and throughput."""
    from echovessel.runtime.turn.coordinator import assemble_turn

    rt = runtime
    models = ["gpt-4o-mini", "gpt-4o"]
    prompt = "Explain gravity in one sentence."

    print("\n" + "=" * 60)
    print("Model Comparison")
    print("=" * 60)

    for model_name in models:
        if hasattr(rt.ctx.llm, "_pinned_model"):
            rt.ctx.llm._pinned_model = model_name

        db, ctx = _build_turn_context(rt)
        turn = _make_turn(prompt, ref=f"perf-cmp-{model_name}")

        ttft: float | None = None
        count = 0
        t0 = time.perf_counter()

        async def on_token(mid: int, delta: str) -> None:
            nonlocal ttft, count
            if ttft is None:
                ttft = time.perf_counter()
            count += 1

        try:
            result = await assemble_turn(ctx, turn, rt.ctx.llm, on_token=on_token)
            total = time.perf_counter() - t0
            ttft_ms = ((ttft - t0) * 1000) if ttft else total * 1000
            tps = count / total if total > 0 else 0

            print(f"  {model_name}:")
            print(f"    Reply:  \"{result.reply[:80]}\"")
            print(f"    TTFT:   {ttft_ms:.0f} ms")
            print(f"    Total:  {total*1000:.0f} ms")
            print(f"    Speed:  {tps:.1f} tok/s")
        except Exception as e:
            print(f"  {model_name}: FAILED — {e}")
        finally:
            db.close()

        await asyncio.sleep(0.5)

    print("=" * 60)
