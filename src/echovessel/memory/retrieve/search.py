"""ConceptNode search + listing for the admin Memory tabs.

Two query surfaces:

- :func:`search_concept_nodes` — FTS5 / LIKE-fallback search over
  ``ConceptNode.description`` with optional ``node_types`` and ``tag``
  filters. Returns ``(hits, total)`` so the admin search UI can render
  ``"showing X of Y"``.
- :func:`list_concept_nodes` — paginated timeline listing of
  ``ConceptNode`` rows for the admin Events / Thoughts tabs.

Neither function filters by ``channel_id`` — see the iron rule in
``echovessel.memory.retrieve.core``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text as _text
from sqlmodel import Session as DbSession
from sqlmodel import func as _func
from sqlmodel import select

from echovessel.core.types import NodeType
from echovessel.memory.models import ConceptNode


@dataclass(slots=True)
class ConceptSearchHit:
    """One row from :func:`search_concept_nodes`.

    ``snippet`` is the FTS5 ``snippet()`` output for ``description`` —
    a short HTML fragment with ``<b>`` tags around the matched terms.
    Front-end is responsible for sanitising / rendering it (the only
    HTML we emit is ``<b>``, see Worker θ tracker).
    """

    node: ConceptNode
    snippet: str
    rank: float


def search_concept_nodes(
    db: DbSession,
    persona_id: str,
    user_id: str,
    *,
    query_text: str,
    node_types: tuple[NodeType, ...] | None = None,
    tag: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[ConceptSearchHit], int]:
    """FTS5 search over ``ConceptNode.description`` with optional filters.

    Returns ``(hits, total)``. ``total`` is the unfiltered match count
    so the admin search UI can render "showing X of Y".

    The query string is sanitised by the same
    ``_sanitize_fts5_query`` helper :class:`SQLiteBackend` uses for L2
    FTS — special characters (AND / OR / NEAR / parens / colons …)
    are wrapped in double quotes and treated as literals. An empty
    query returns ``([], 0)`` rather than running an unbounded scan.

    ``tag`` matches ``emotion_tags`` OR ``relational_tags`` JSON
    arrays. Worker θ scope: a single tag string, exact match (``LIKE
    '%"<tag>"%'`` against the JSON serialisation). The DB schema
    doesn't have a tag-junction table; if richer queries are ever
    needed, that's the time to introduce one.
    """

    if not query_text or not query_text.strip():
        return [], 0

    # Reuse the proven helper — keep the sanitisation identical between
    # L2 and concept search so users see the same query-syntax behaviour.
    from echovessel.memory.backends.sqlite import SQLiteBackend

    safe_query = SQLiteBackend._sanitize_fts5_query(query_text)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    # SQLite trigram tokenizer can't match terms shorter than 3
    # characters — common case for CJK queries like "下雨" (2 chars).
    # When any token is too short, fall back to a plain ``LIKE`` scan
    # over ``concept_nodes.description``. Snippets are still produced,
    # just hand-rolled instead of via FTS5's ``snippet()``.
    stripped = query_text.strip()
    tokens = [t for t in stripped.replace('"', " ").split() if t]
    use_like_fallback = any(len(t) < 3 for t in tokens) or not tokens

    type_values: tuple[str, ...] = tuple(t.value for t in node_types) if node_types else ()

    type_clause = ""
    if type_values:
        # Build ``cn.type IN (:t0, :t1, ...)`` dynamically — the IN tuple
        # length is unknown so we expand at format time.
        placeholders = ", ".join(f":type_{i}" for i in range(len(type_values)))
        type_clause = f"AND cn.type IN ({placeholders})"

    tag_clause = ""
    if tag:
        # JSON arrays are stored as plain TEXT in SQLite. Match either
        # emotion_tags OR relational_tags containing the literal tag.
        # We wrap the search in quotes so partial overlaps don't match
        # (e.g. searching for "joy" won't match "joyful").
        tag_clause = "AND (cn.emotion_tags LIKE :tag_pat OR cn.relational_tags LIKE :tag_pat)"

    if use_like_fallback:
        # LIKE path — stable across CJK / short queries, no FTS5 trigram
        # length floor. Snippet is hand-rolled in
        # :func:`_build_like_snippet` below.
        like_pattern = f"%{tokens[0]}%" if tokens else "%"
        page_sql = _text(
            f"""
            SELECT cn.id, cn.description
            FROM concept_nodes AS cn
            WHERE cn.description LIKE :like_pat
              AND cn.persona_id = :persona_id
              AND cn.user_id = :user_id
              AND cn.deleted_at IS NULL
              {type_clause}
              {tag_clause}
            ORDER BY cn.created_at DESC
            LIMIT :limit OFFSET :offset
            """
        )
        count_sql = _text(
            f"""
            SELECT COUNT(*)
            FROM concept_nodes AS cn
            WHERE cn.description LIKE :like_pat
              AND cn.persona_id = :persona_id
              AND cn.user_id = :user_id
              AND cn.deleted_at IS NULL
              {type_clause}
              {tag_clause}
            """
        )
        params: dict = {
            "like_pat": like_pattern,
            "persona_id": persona_id,
            "user_id": user_id,
            "limit": limit,
            "offset": offset,
        }
    else:
        page_sql = _text(
            f"""
            SELECT
                cn.id,
                snippet(concept_nodes_fts, 0, '<b>', '</b>', '…', 16) AS snip,
                fts.rank
            FROM concept_nodes_fts AS fts
            JOIN concept_nodes AS cn ON cn.id = fts.rowid
            WHERE concept_nodes_fts MATCH :q
              AND cn.persona_id = :persona_id
              AND cn.user_id = :user_id
              AND cn.deleted_at IS NULL
              {type_clause}
              {tag_clause}
            ORDER BY fts.rank
            LIMIT :limit OFFSET :offset
            """
        )
        count_sql = _text(
            f"""
            SELECT COUNT(*)
            FROM concept_nodes_fts AS fts
            JOIN concept_nodes AS cn ON cn.id = fts.rowid
            WHERE concept_nodes_fts MATCH :q
              AND cn.persona_id = :persona_id
              AND cn.user_id = :user_id
              AND cn.deleted_at IS NULL
              {type_clause}
              {tag_clause}
            """
        )
        params = {
            "q": safe_query,
            "persona_id": persona_id,
            "user_id": user_id,
            "limit": limit,
            "offset": offset,
        }

    for i, v in enumerate(type_values):
        params[f"type_{i}"] = v
    if tag:
        params["tag_pat"] = f'%"{tag}"%'

    page_rows = db.exec(page_sql, params=params).all()  # type: ignore[arg-type]
    count_row = db.exec(count_sql, params=params).first()  # type: ignore[arg-type]
    total = int(count_row[0]) if count_row else 0

    if not page_rows:
        return [], total

    # Hydrate ConceptNode rows in one query, preserving FTS rank order.
    node_ids = [r[0] for r in page_rows]
    node_map = {
        n.id: n
        for n in db.exec(
            select(ConceptNode).where(
                ConceptNode.id.in_(node_ids),  # type: ignore[union-attr]
            )
        )
    }

    hits: list[ConceptSearchHit] = []
    for row in page_rows:
        node = node_map.get(row[0])
        if node is None or node.deleted_at is not None:
            continue
        if use_like_fallback:
            snippet = _build_like_snippet(str(row[1] or ""), tokens[0] if tokens else "")
            rank = 0.0
        else:
            snippet = str(row[1] or "")
            rank = float(row[2])
        hits.append(ConceptSearchHit(node=node, snippet=snippet, rank=rank))

    # Suppress unused symbol warning — keep the helper around for future
    # consumers that want raw counts.
    _ = _func
    return hits, total


def _build_like_snippet(description: str, term: str, *, window: int = 28) -> str:
    """Hand-roll an FTS5-like snippet for the LIKE-fallback path.

    Surrounds the first occurrence of ``term`` with ``<b>…</b>`` and
    truncates with ``…`` on either side so the result fits one line in
    the admin search UI. Term comparison is case-insensitive on ASCII
    but exact for non-ASCII (Python ``str.lower`` over CJK is identity,
    which matches what users expect — searching "下雨" should not match
    a different unicode-equivalent form).
    """

    if not term or not description:
        return description
    haystack_lower = description.lower()
    term_lower = term.lower()
    idx = haystack_lower.find(term_lower)
    if idx < 0:
        return description

    end = idx + len(term)
    left = max(0, idx - window)
    right = min(len(description), end + window)

    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(description) else ""
    head = description[left:idx]
    match = description[idx:end]
    tail = description[end:right]
    return f"{prefix}{head}<b>{match}</b>{tail}{suffix}"


def list_concept_nodes(
    db: DbSession,
    persona_id: str,
    user_id: str,
    *,
    node_type: NodeType,
    limit: int = 20,
    offset: int = 0,
    subject: str | None = None,
) -> tuple[list[ConceptNode], int]:
    """Pure ConceptNode timeline query for the admin Memory tabs.

    Returns ``(rows, total_count)`` for ``(persona_id, user_id, node_type)``
    ordered by ``created_at`` DESC, excluding soft-deleted rows. The
    admin Events / Thoughts tabs use this to render a paginated list
    with the server-side total — total is computed separately so the
    UI can show "showing X of Y" without bringing every row over the
    wire.

    🚨 Same iron rule as :func:`list_recall_messages`: this function
    does NOT accept a channel_id parameter. There is no transport
    filtering at the memory layer.

    Args:
        db: SQLModel session.
        persona_id: Whose timeline.
        user_id: For which user (MVP: always "self").
        node_type: ``NodeType.EVENT`` for the Events tab,
            ``NodeType.THOUGHT`` for the Thoughts tab.
        limit: Max rows returned, hard-capped at 100.
        offset: Number of rows to skip from the head of the DESC order
            (i.e. "page through older entries"). Negative values are
            clamped to 0.
        subject: v0.5 hotfix · optional filter on ``ConceptNode.subject``
            (``'user'`` / ``'persona'`` / ``'shared'``). ``None`` keeps
            the legacy behaviour (no filter). Used by the admin
            Persona tab to surface only ``subject='persona'`` thoughts
            for the Reflection section.

    Returns:
        ``(rows, total)`` where ``rows`` is a list of ConceptNode in
        DESCENDING created_at order and ``total`` is the unfiltered
        non-deleted count for the same persona/user/node_type triple.
    """

    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    base_filters: tuple = (
        ConceptNode.persona_id == persona_id,
        ConceptNode.user_id == user_id,
        ConceptNode.type == node_type,
        ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
    )
    if subject is not None:
        base_filters = (*base_filters, ConceptNode.subject == subject)

    page_stmt = (
        select(ConceptNode)
        .where(*base_filters)
        .order_by(ConceptNode.created_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
        .offset(offset)
    )
    rows = list(db.exec(page_stmt).all())

    total_stmt = select(_func.count()).select_from(ConceptNode).where(*base_filters)
    total = int(db.exec(total_stmt).one() or 0)

    return rows, total
