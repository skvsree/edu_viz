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
    topics: list[RevisionTopic] | None = None,
) -> _GraphData | None:
    """Given extracted text, produce the concept map graph.

    When ``topics`` is provided (pre-built, optionally enriched with
    AI-selected bullets), uses those directly — skipping the heuristic.
    Otherwise runs the two-pass heuristic on ``source_text``.

    Returns None if no topics are found.
    """
    if topics is None:
        if not source_text or len(source_text.strip()) < 200:
            return None
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
    credential_provider_name: str | None = None,
    credential: object | None = None,
) -> ConceptMap:
    """Generate a concept map for a deck.

    Hard-regenerate: always overwrites any existing map. Updates in-place
    so the ConceptMap UUID stays stable for Job references.

    When ``credential`` is provided, runs AI-as-selector on each topic
    to pick verbatim key-point bullets (same pipeline as revision notes).
    Otherwise uses heuristic-only key-point extraction.

    Parameters:
        deck_id: The deck to attach the map to.
        source_file_id: Optional source file for provenance.
        source_text: Pre-extracted text. If None, reads from source file.
        title: Display title for the map (e.g. chapter name).
        credential_provider_name: AI provider name for key-point selection.
        credential: AI credential object for key-point selection.

    Returns the ConceptMap row (persisted).
    """
    # Upsert: reuse existing row so UUID is stable for Job references.
    # Hard-regenerate: always overwrite, never skip.
    existing = (
        db.query(ConceptMap)
        .filter(ConceptMap.deck_id == deck_id)
        .first()
    )
    if existing:
        concept_map = existing
        concept_map.source_file_id = source_file_id
        concept_map.status = ConceptMapStatus.PROCESSING.value
        concept_map.error_message = None
        concept_map.graph_data = None
        concept_map.title = None
        concept_map.node_count = None
        concept_map.started_at = datetime.utcnow()
        concept_map.completed_at = None
    else:
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
        # Run the two-pass heuristic to get topics
        topics = heuristic_topics(source_text, max_topics=12)

        # AI-as-selector: enrich topics with verbatim key points
        if credential is not None and hasattr(credential, "secret") and credential.secret:
            from app.services.revision_notes import (
                select_topic_recall_bullets,
            )
            for topic in topics:
                if not topic.source_paragraphs:
                    continue
                try:
                    bullets, used = select_topic_recall_bullets(
                        db,
                        topic_title=topic.title,
                        source_paragraphs=topic.source_paragraphs,
                        credential_provider_name=credential_provider_name or "openai",
                        credential=credential,
                    )
                    if used:
                        topic.ai_bullets = bullets
                        topic.ai_used = True
                except Exception:
                    pass  # fall back to heuristic key points below

        graph = _build_concept_map_data(source_text, title, topics=topics)
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


# ---------------------------------------------------------------------------
# Revision notes PDF
# ---------------------------------------------------------------------------

REVISION_PDF_KEY_TEMPLATE = "concept_maps/{concept_map_id}/revision_notes.pdf"
MIN_REVISION_SOURCE_CHARS = 200


def revision_pdf_key(concept_map_id: uuid.UUID) -> str:
    return REVISION_PDF_KEY_TEMPLATE.format(concept_map_id=concept_map_id)


def _concept_map_source_text(
    db: Session, concept_map: ConceptMap, *, max_chars: int
) -> str:
    """Source text for revision notes: the upload it came from, else its cards.

    Concept maps generated from an uploaded chapter have a source file with the
    extracted text. Maps generated for a hand-built deck have none, so the
    deck's own cards are used instead — that is what makes an on-demand
    download possible for every deck.
    """
    source_file_id = getattr(concept_map, "source_file_id", None)
    if source_file_id:
        file_row = db.get(BulkAIUploadFile, source_file_id)
        if file_row is not None:
            text = _extracted_payload(file_row)
            if text and text.strip():
                return text[:max_chars]

    from app.models import Card

    cards = (
        db.query(Card)
        .filter(Card.deck_id == concept_map.deck_id)
        .order_by(Card.created_at)
        .limit(600)
        .all()
    )
    parts: list[str] = []
    for card in cards:
        front = (card.front or "").strip()
        back = (card.back or "").strip()
        joined = "\n".join(part for part in (front, back) if part)
        if joined:
            parts.append(joined)
    return "\n\n".join(parts)[:max_chars]


def generate_concept_map_revision_pdf(
    db: Session,
    *,
    concept_map_id: uuid.UUID,
    credential_provider_name: str | None = None,
    credential: object | None = None,
) -> ConceptMap:
    """Build (or rebuild) the revision notes PDF for a deck's concept map.

    Mirrors the bulk-upload pipeline in ``revision_notes.py``: AI-first topic
    selection with a heuristic + verbatim-selector fallback, rendered through
    the same low-ink palette and stored under
    ``concept_maps/<concept_map id>/revision_notes.pdf``.

    ``ConceptMap.revision_pdf_status`` ends as ``ready`` or ``failed``;
    generation errors are recorded on the row and in ``error_message`` rather
    than raised, so the caller can simply look at the status.
    """
    from app.core.config import settings
    from app.services import revision_notes as RN

    concept_map = db.get(ConceptMap, concept_map_id)
    if concept_map is None:
        raise ValueError(f"Concept map {concept_map_id} not found")

    from app.models import Deck

    deck = db.get(Deck, concept_map.deck_id) if concept_map.deck_id else None
    deck_name = (getattr(deck, "name", None) or "").strip() or "Deck"

    concept_map.revision_pdf_status = "processing"
    concept_map.error_message = None
    db.commit()

    page_count = 0
    used_ai = False
    try:
        from app.services.storage import get_storage, guess_content_type

        source_text = _concept_map_source_text(
            db,
            concept_map,
            max_chars=settings.revision_notes_max_source_chars,
        )
        if len(source_text.strip()) < MIN_REVISION_SOURCE_CHARS:
            raise ValueError(
                "Not enough content to build revision notes "
                f"({len(source_text.strip())} characters)"
            )

        chapter_label = (concept_map.title or deck_name or "Revision Notes").strip()
        topics: list[RevisionTopic] = []
        final_sentence = ""

        if credential is not None and getattr(credential, "secret", None):
            try:
                topics, final_sentence, used_ai = RN.generate_ai_topics(
                    db,
                    source_text=source_text,
                    chapter_label=chapter_label,
                    credential_provider_name=credential_provider_name or "openai",
                    credential=credential,
                    session_id=RN.opencode_session_id(f"rev-cm-{concept_map.id}"),
                )
            except Exception as exc:  # noqa: BLE001 - heuristic fallback below
                logger.warning(
                    "concept_map: AI topics failed for %s, using heuristic: %s",
                    concept_map.id,
                    exc,
                )
                topics = []

        if not topics:
            topics = [
                topic
                for topic in heuristic_topics(source_text, max_topics=9)
                if topic.sections
            ]
            topics = RN._attach_payloads(source_text, topics)

        if not topics:
            raise ValueError("Could not extract topics from the source text")

        doc = RN._build_revision_doc(
            deck_name=deck_name,
            chapter_label=chapter_label,
            source_title=chapter_label,
            subtitle=(
                (getattr(deck, "description", None) or "")[:240] if deck else ""
            ),
            topics=topics,
            final_sentence=final_sentence
            or f"Generated on {datetime.utcnow().strftime('%Y-%m-%d')}.",
        )
        pdf_bytes, page_count = RN.render_revision_pdf(doc, deck_name)

        key = revision_pdf_key(concept_map.id)
        get_storage().save_bytes(
            key=key,
            data=pdf_bytes,
            content_type=guess_content_type(".pdf") or "application/pdf",
        )
        concept_map.revision_pdf_storage_key = key
        concept_map.revision_pdf_status = "ready"
        concept_map.completed_at = datetime.utcnow()
        logger.info(
            "concept_map: revision PDF ready deck=%s pages=%d bytes=%d ai=%s",
            concept_map.deck_id,
            page_count,
            len(pdf_bytes),
            used_ai,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "concept_map: revision PDF failed for map %s", concept_map_id
        )
        concept_map.revision_pdf_status = "failed"
        concept_map.error_message = f"Revision notes generation failed: {exc}"[:500]

    db.commit()
    return concept_map
