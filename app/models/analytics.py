import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class AnalyticsEventType(PyEnum):
    REVIEW_COMPLETED = "review_completed"
    TEST_STARTED = "test_started"
    TEST_COMPLETED = "test_completed"
    DECK_CREATED = "deck_created"
    CARD_CREATED = "card_created"
    AI_UPLOAD_PROCESSED = "ai_upload_processed"
    MCQ_GENERATED = "mcq_generated"
    TAG_ASSIGNED = "tag_assigned"


class UserAnalytics(Base):
    __tablename__ = "user_analytics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    total_reviews: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    correct_reviews: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    accuracy_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_study_time_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    average_session_minutes: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    study_streak_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    decks_studied: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cards_studied: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_cards_learned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tests_taken: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    average_test_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    ai_generated_cards_studied: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mcqs_practiced: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class OrganizationAnalytics(Base):
    __tablename__ = "organization_analytics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    total_users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_reviews: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    correct_reviews: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    organization_accuracy_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_study_time_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    average_study_time_per_user: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_decks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cards: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    decks_studied: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tests_taken: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    average_test_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    ai_generated_content_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class SystemAnalytics(Base):
    __tablename__ = "system_analytics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    total_organizations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    system_admins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_reviews: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    correct_reviews: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    system_accuracy_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_study_time_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    average_study_time_per_user: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_decks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cards: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_ai_generated_cards: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_mcqs_generated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tests_taken: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    average_test_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class AnalyticsEvent(Base):
    __tablename__ = "analytics_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True, index=True)
    event_type: Mapped[AnalyticsEventType] = mapped_column(Enum(AnalyticsEventType), nullable=False, index=True)
    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True)
    event_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    user = relationship("User", foreign_keys=[user_id])
    organization = relationship("Organization", foreign_keys=[organization_id])