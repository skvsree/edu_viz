import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    is_ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_test_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    test_daily_limit: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0 = use env default
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    users = relationship("User", back_populates="organization")
    decks = relationship("Deck", back_populates="organization")
    tags = relationship("Tag", back_populates="organization")
