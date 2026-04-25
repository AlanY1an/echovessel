"""Universal importer pipeline (tracker `docs/import/03-code-tracker.md`).

Layer 2 subsystem. Depends only on ``echovessel.memory`` and
``echovessel.core``. Does **not** import from ``runtime`` / ``channels`` /
``proactive`` — the runtime glue lives in
``echovessel.runtime.wiring.importer`` and injects every cross-layer
collaborator by parameter.

Public surface:

    from echovessel.import_ import (
        run_pipeline,
        PipelineReport,
        PipelineEventLike,
        ContentItem,
        Chunk,
        DroppedItem,
        PipelineError,
        NormalizationError,
        ExtractionError,
        RoutingError,
        EmbedError,
        ChunkingError,
    )
"""

from echovessel.import_.errors import (
    ChunkingError,
    EmbedError,
    ExtractionError,
    NormalizationError,
    PipelineError,
    RoutingError,
)
from echovessel.import_.models import (
    ALLOWED_CONTENT_TYPES,
    Chunk,
    ContentItem,
    DroppedItem,
    PipelineReport,
    ProgressSnapshot,
)
from echovessel.import_.pipeline import (
    PipelineEventLike,
    run_pipeline,
)

__all__ = [
    # errors
    "PipelineError",
    "NormalizationError",
    "ChunkingError",
    "ExtractionError",
    "RoutingError",
    "EmbedError",
    # models
    "ALLOWED_CONTENT_TYPES",
    "Chunk",
    "ContentItem",
    "DroppedItem",
    "PipelineReport",
    "ProgressSnapshot",
    # pipeline
    "PipelineEventLike",
    "run_pipeline",
]
