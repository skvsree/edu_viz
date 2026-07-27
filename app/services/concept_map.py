"""Concept map generation service.

Takes extracted text from a BulkAIUploadFile and produces a hierarchical
graph (nodes + edges) representing chapters → topics → key points.

The graph data is stored as JSONB on the ConceptMap model and rendered
as an interactive mind map in the frontend.

Design:
- Reuses the two-pass heuristic from revision_notes.py (chapter detection
  + sub-topic detection) so the map structure matches the revision PDF
- Falls back to chunked topics when the heuristic can't find headings
- Extracts key-point sentences from each topic's source paragraphs for
  leaf-level detail nodes
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import (
    BulkAIUploadFile,
    ConceptMap,
    ConceptMapStatus,
)
from app.services.revision_notes import (
    RevisionTopic,
    _detect_chapters,
    _split_paragraphs,
    _subtopics_within_chapter,
    _chunked_topics,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graph builders
# ---------------------------------------------------------------------------
_GraphNode = dict
_GraphEdge = dict
_GraphData = dict[str, list]  # {nodes: [...], edges: [...]}


def _make_node(
    node_id: str,
    node_type: str,
    label: str,
    depth: int,
    parent: str | None = None,
    **extra,
) -> _GraphNode:
    node: _GraphNode = {
        "id": node_id,
        "type": node_type,
        "label": _sanitise_label(label),
        "depth": depth,
    }
    if parent:
        node["parent"] = parent
    node.update(extra)
    return node


def _make_edge(source: str, target: str, relation: str = "contains") -> _GraphEdge:
    return {"source": source, "target": target, "relation": relation}


def _sanitise_label(text: str) -> str:
    """Clean up labels for display. Strip very long text, collapse
    whitespace, and limit to readable length."""
    text = " ".join((text or "").split())
    if len(text) > 120:
        text = text[:117] + "..."
    return text


def _extract_keypoints(
    paragraphs: list[str], max_points: int = 3
) -> list[str]:
    """Extract the most important sentences from a topic's source paragraphs.

    Picks the first substantive sentence from each paragraph (up to
    max_points) as key points for leaf nodes. Prefers sentences that
    look like actual content (not headings, not lists).
    """
    points: list[str] = []
    for p in paragraphs:
        if len(points) >= max_points:
            break
        if not p or len(p) < 40:
            continue
        # Reject heading-like lines
        first_line = p.split("\n", 1)[0].strip()
        if len(first_line.split()) <= 8 and first_line[0:1].isupper():
            continue
        # Reject numbered items / bullet lists
        if re.match(r"^\d+[.)]\s", p.strip()):
            continue
        # Take first sentence
        sentences = re.split(r"(?<=[.!?])\s+", p.strip())
        best = sentences[0] if sentences else p
        if len(best) > 200:
            best = best[:197] + "..."
        if len(best) >= 50:
            points.append(best)
    return points


def _topics_to_graph(
    title: str,
    topics: list[RevisionTopic],
) -> _GraphData:
    """Convert a list of RevisionTopic objects into a graph structure.

    Builds a tree where:
      root -> chapters (when present) -> topics -> keypoints
      root -> topics (when no chapters) -> keypoints

    Topics with the same ``chapter`` value are grouped under a chapter
    node. Topics without a chapter are placed directly under root.
    """
    nodes: list[_GraphNode] = []
    edges: list[_GraphEdge] = []

    root_id = "root"
    nodes.append(_make_node(root_id, "root", title, depth=0))

    # Group topics by chapter
    chapter_groups: dict[str, list[RevisionTopic]] = {}
    flat_topics: list[RevisionTopic] = []
    for t in topics:
        chap = getattr(t, "chapter", None) or ""
        if chap:
            chapter_groups.setdefault(chap, []).append(t)
        else:
            flat_topics.append(t)

    node_counter = [0]  # mutable counter for unique IDs

    def next_id(prefix: str = "n") -> str:
        node_counter[0] += 1
        return f"{prefix}_{node_counter[0]}"

    # Render chapter groups
    for chap_title, chap_topics in chapter_groups.items():
        chap_id = next_id("ch")
        nodes.append(
            _make_node(chap_id, "chapter", chap_title, depth=1, parent=root_id)
        )
        edges.append(_make_edge(root_id, chap_id, "contains"))

        for t in chap_topics:
            topic_id = next_id("t")
            nodes.append(
                _make_node(topic_id, "topic", t.title, depth=2, parent=chap_id)
            )
            edges.append(_make_edge(chap_id, topic_id, "contains"))
            _attach_keypoints(t, topic_id, nodes, edges)

    # Render flat topics (no chapter detected)
    for t in flat_topics:
        topic_id = next_id("t")
        nodes.append(
            _make_node(topic_id, "topic", t.title, depth=1, parent=root_id)
        )
        edges.append(_make_edge(root_id, topic_id, "contains"))
        _attach_keypoints(t, topic_id, nodes, edges)

    return {"nodes": nodes, "edges": edges}


def _attach_keypoints(
    topic: RevisionTopic,
    topic_id: str,
    nodes: list[_GraphNode],
    edges: list[_GraphEdge],
    max_points: int = 3,
) -> None:
    """Add key-point leaf nodes under a topic node."""
    # Prefer AI-selected bullets (they're verbatim key points)
    if getattr(topic, "ai_bullets", None) and topic.ai_used:
        sources = topic.ai_bullets
    elif topic.source_paragraphs:
        sources = _extract_keypoints(topic.source_paragraphs, max_points)
    else:
        sources = []

    for i, point in enumerate(sources):
        if not point or len(point.strip()) < 30:
            continue
        kp_id = f"{topic_id}_kp_{i}"
        nodes.append(
            _make_node(
                kp_id, "keypoint", point.strip(),
                depth=3 if getattr(topic, "chapter", None) else 2,
                parent=topic_id,
            )
        )
        edges.append(_make_edge(topic_id, kp_id, "notes"))
        if i >= max_points - 1:
            break


# ---------------------------------------------------------------------------
# High-level entry: orchestrate extraction -> graph -> persist
# ---------------------------------------------------------------------------


def _extracted_payload(file_row: BulkAIUploadFile) -> str:
    """Get the source text. Prefer the DB column; fall back to storage."""
    if getattr(file_row, "content_text", None):
        return file_row.content_text or ""
    if getattr(file_row, "storage_key", None):
        from app.services.storage import StorageError, get_storage

        storage = get_storage()
        try:
            data, _ctype = storage.open_bytes(key=file_row.storage_key)
            if (file_row.storage_key or "").lower().endswith(".pdf"):
                from app.api.routers.bulk_ai_upload import extract_text_from_pdf

                return extract_text_from_pdf(data)
            return data.decode("utf-8", errors="replace")
        except StorageError as exc:
            logger.warning(
                "concept_map: storage read failed for %s: %s",
                file_row.storage_key,
                exc,
            )
    return ""


def _build_concept_map_data(
    source_text: str,
    title: str,
    max_topics: int = 12,
    chapter_label: str = "",
) -> _GraphData | None:
    """Given extracted text, produce the concept map graph.

    Returns None if the text is too short to extract anything useful.
    """
    if not source_text or len(source_text.strip()) < 200:
        return None

    # Run the two-pass heuristic to get topics with chapter grouping
    topics = heuristic_topics(source_text, max_topics=max_topics)

    if not topics:
        return None

    # Build the graph
    graph = _topics_to_graph(title or chapter_label or "Chapter", topics)
    return graph


def heuristic_topics(
    text: str, *, max_topics: int = 12
) -> list[RevisionTopic]:
    """Wrapper around revision_notes.heuristic_topics that falls back
    gracefully. Re-exports the two-pass logic."""
    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        return []

    chapter_blocks = _detect_chapters(paragraphs)
    if not chapter_blocks:
        all_subtopics = _subtopics_within_chapter(
            chapter_title="", paras=paragraphs, max_per_chapter=max_topics
        )
        if len(all_subtopics) >= 4:
            return all_subtopics
        return _chunked_topics(paragraphs, max_topics=max_topics)

    topics: list[RevisionTopic] = []
    for chap_title, chap_paras in chapter_blocks:
        sub_topics = _subtopics_within_chapter(
            chap_title, chap_paras, max_per_chapter=max_topics
        )
        topics.extend(sub_topics)
        if len(topics) >= max_topics:
            break

    if len(topics) < 4:
        flat = _chunked_topics(paragraphs, max_topics=max_topics)
        if len(flat) > len(topics):
            return flat
    return topics[:max_topics]


def generate_concept_map(
    db: Session,
    *,
    deck_id: uuid.UUID,
    source_file_id: uuid.UUID | None = None,
    source_text: str | None = None,
    title: str = "",
) -> ConceptMap:
    """Generate a concept map for a deck.

    Parameters:
        deck_id: The deck to attach the map to.
        source_file_id: Optional source file for provenance.
        source_text: Pre-extracted text. If None, reads from source file.
        title: Display title for the map (e.g. chapter name).

    Returns the ConceptMap row (persisted).
    """
    # Mark any existing concept map row as stale
    existing = (
        db.query(ConceptMap)
        .filter(ConceptMap.deck_id == deck_id)
        .first()
    )
    if existing:
        existing.status = ConceptMapStatus.FAILED.value
        existing.error_message = "Superseded by new generation"
        db.flush()

    concept_map = ConceptMap(
        deck_id=deck_id,
        source_file_id=source_file_id,
        status=ConceptMapStatus.PROCESSING.value,
        started_at=datetime.utcnow(),
    )
    db.add(concept_map)
    db.flush()

    # Resolve source text if not provided
    if source_text is None and source_file_id:
        file_row = db.get(BulkAIUploadFile, source_file_id)
        if file_row:
            source_text = _extracted_payload(file_row)

    if not source_text:
        concept_map.status = ConceptMapStatus.FAILED.value
        concept_map.error_message = "No source text available"
        db.commit()
        return concept_map

    try:
        graph = _build_concept_map_data(source_text, title)
        if graph is None or not graph.get("nodes"):
            concept_map.status = ConceptMapStatus.FAILED.value
            concept_map.error_message = (
                f"Could not extract topics from source text "
                f"({len(source_text)} chars)"
            )
            db.commit()
            return concept_map

        concept_map.graph_data = graph
        concept_map.title = title or graph["nodes"][0]["label"]
        concept_map.node_count = len(graph["nodes"])
        concept_map.status = ConceptMapStatus.READY.value
        concept_map.completed_at = datetime.utcnow()

        logger.info(
            "concept_map: ready deck=%s nodes=%d edges=%d title=%s",
            deck_id,
            len(graph["nodes"]),
            len(graph["edges"]),
            concept_map.title,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("concept_map: generation failed for deck %s", deck_id)
        concept_map.status = ConceptMapStatus.FAILED.value
        concept_map.error_message = f"Generation failed: {exc}"

    db.commit()
    return concept_map
