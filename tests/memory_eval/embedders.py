"""LLM + embedder wiring for eval runs.

Two embedders ship: the production sentence-transformers model (when
the extra is installed) and a deterministic keyword-axis fallback for
hosts without the 90 MB download. The LLM provider is read from
``~/.echovessel/config.toml`` so eval exercises whatever the user
actually configured.
"""

from __future__ import annotations

import os
from pathlib import Path

from echovessel.runtime.config import load_config
from echovessel.runtime.llm.base import LLMProvider
from echovessel.runtime.llm.factory import build_llm_provider

REAL_CONFIG_PATH = Path.home() / ".echovessel" / "config.toml"


def build_live_llm() -> LLMProvider:
    """Read ``~/.echovessel/config.toml`` to build the user's daemon
    provider. Callers should ``pytest.skip`` on KeyError/FileNotFound so
    eval tests degrade gracefully on CI or a fresh clone.
    """

    if not REAL_CONFIG_PATH.is_file():
        raise FileNotFoundError(
            f"eval needs a real LLM config at {REAL_CONFIG_PATH}"
        )
    cfg = load_config(REAL_CONFIG_PATH)
    if cfg.llm.provider == "stub":
        raise RuntimeError(
            "eval requires a non-stub provider in config.llm.provider"
        )
    if cfg.llm.api_key_env and not os.environ.get(cfg.llm.api_key_env):
        raise RuntimeError(
            f"eval needs {cfg.llm.api_key_env} set in the environment"
        )
    return build_llm_provider(cfg.llm)


def build_eval_embedder():
    """Pick the best embedder available for this eval run.

    Preference order:
      1. ``sentence-transformers`` (real semantic embeddings). First
         call downloads the ~90 MB ``all-MiniLM-L6-v2`` model into
         ``~/.echovessel/embedder.cache/``; subsequent runs are
         instantaneous. This is the same embedder the daemon uses in
         production, so retrieve-relevance invariants are meaningful.
      2. Keyword-axis fallback. Cheap + deterministic, but groups
         tokens by hand-curated axis so synonyms that were not
         enumerated miss each other. Triggered automatically when
         ``sentence-transformers`` is not installed.

    Honours ``ECHOVESSEL_EVAL_EMBEDDER=keyword`` as an override so a
    debugging run can skip the heavy load explicitly.
    """

    if os.environ.get("ECHOVESSEL_EVAL_EMBEDDER") == "keyword":
        return keyword_embedder()[0]

    try:
        from echovessel.runtime import build_sentence_transformers_embedder
    except ImportError:
        return keyword_embedder()[0]

    cache_dir = Path.home() / ".echovessel" / "embedder.cache"
    try:
        return build_sentence_transformers_embedder(
            model_name="all-MiniLM-L6-v2", cache_dir=cache_dir
        )
    except ImportError:
        # ``sentence-transformers`` extra not installed on this host.
        return keyword_embedder()[0]


def keyword_embedder() -> tuple[callable, dict[str, int]]:
    """Return ``(embed_fn, axes)`` — a deterministic keyword-axis
    embedder plus the axis map it uses.

    Used as the fallback when ``sentence-transformers`` is not
    available. Keeps eval runnable on a bare-bones clone without the
    90 MB model download.

    Each keyword gets its own axis; texts that mention multiple
    keywords land in the mean-vector. Unknown texts hash into a
    fallback slot so vectors never collapse to all-zero.
    """

    axes: dict[str, int] = {}
    dim = 384

    keywords = [
        # E1
        "张丽华", "老伴", "丧偶", "过世", "退休", "沈阳", "中学", "语文",
        "Mochi", "mochi", "黑猫", "猫", "领养", "2020",
        # E3
        "妈", "母亲", "走了", "没说",
        # E4
        "28", "32", "成都", "更正", "说错",
        # E5
        "分手", "前任", "新工作", "新城市", "失眠", "朋友",
        # E6
        "意大利面", "羽毛球", "生日", "fintech", "室友", "医院",
        "东京", "吉他",
        # E7
        "难受", "工作", "压", "没人能说", "喘不过气",
        # E8
        "画展", "印象派", "莫奈", "睡莲", "calm",
    ]
    for i, kw in enumerate(keywords):
        axes[kw.lower()] = i % (dim - 16)

    def _embed(text: str) -> list[float]:
        v = [0.0] * dim
        low = text.lower()
        matched = False
        for kw, axis in axes.items():
            if kw in low:
                v[axis] += 1.0
                matched = True
        if not matched:
            v[(abs(hash(text)) % 16) + (dim - 16)] = 1.0
        # normalise
        norm = sum(x * x for x in v) ** 0.5
        if norm > 0:
            v = [x / norm for x in v]
        return v

    return _embed, axes


__all__ = [
    "REAL_CONFIG_PATH",
    "build_eval_embedder",
    "build_live_llm",
    "keyword_embedder",
]
