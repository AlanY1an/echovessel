"""Phase A · trivial-skip judgement (architecture v0.3 §3.3 part A).

A session is "trivial" when it falls below both message-count and
token-count thresholds AND contains no strong-emotion keyword. Trivial
sessions short-circuit the consolidate pipeline (mark CLOSED, no
extraction LLM call) so we don't spend tokens on idle chatter.

Strong-emotion override exists because a single late-night line —
"我妈走了" / "撑不住了" — is exactly the moment proactive policy
needs to see, even when the session length is below threshold.
"""

from __future__ import annotations

from echovessel.memory.models import RecallMessage, Session

# Trivial skip thresholds
TRIVIAL_MESSAGE_COUNT = 3
TRIVIAL_TOKEN_COUNT = 200

# Strong-emotion keyword override for the trivial skip rule.
# Architecture §3.3 Part A calls this out explicitly: even trivial sessions
# must be extracted if they contain high-emotion signals, so that Proactive
# Policy can see "user sent one sad message at midnight and went silent".
# MVP is a small hardcoded Chinese+English list; v1.x can expand or use a
# lightweight classifier.
STRONG_EMOTION_KEYWORDS: tuple[str, ...] = (
    # Bereavement / loss
    "走了",
    "去世",
    "死了",
    "离世",
    "葬礼",
    "没了",
    "died",
    "passed away",
    "funeral",
    # Crisis
    "撑不住",
    "不想活",
    "活不下去",
    "自杀",
    "崩溃",
    "can't go on",
    "suicide",
    "breakdown",
    # Major milestones
    "分手",
    "离婚",
    "被裁",
    "breakup",
    "divorce",
    "fired",
)


def _has_strong_emotion(messages: list[RecallMessage]) -> bool:
    """Return True if any message contains a strong-emotion keyword."""
    for m in messages:
        content_lower = m.content.lower()
        for kw in STRONG_EMOTION_KEYWORDS:
            if kw.lower() in content_lower:
                return True
    return False


def is_trivial(
    session: Session,
    messages: list[RecallMessage],
    *,
    trivial_message_count: int = TRIVIAL_MESSAGE_COUNT,
    trivial_token_count: int = TRIVIAL_TOKEN_COUNT,
) -> bool:
    """Decide whether to skip extraction for this session.

    Returns True iff the session is below the message/token thresholds AND
    contains no strong-emotion keywords. Strong emotion always forces
    extraction even when the session is tiny (e.g. a single late-night line).

    The two threshold arguments default to the module-level constants so
    existing callers are behaviour-preserving. Runtime threads them from
    ``cfg.consolidate.trivial_message_count`` /
    ``cfg.consolidate.trivial_token_count`` via
    :class:`echovessel.runtime.loops.consolidate_worker.ConsolidateWorker`.
    """
    if session.message_count >= trivial_message_count:
        return False
    if session.total_tokens >= trivial_token_count:
        return False
    return not _has_strong_emotion(messages)
