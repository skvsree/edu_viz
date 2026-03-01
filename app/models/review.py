import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    card_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("cards.id"), index=True, nullable=False)

    rating: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..4
    review_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    scheduled_days: Mapped[int] = mapped_column(Integer, nullable=False)
    elapsed_days: Mapped[int] = mapped_column(Integer, nullable=False)

    card = relationship("Card", back_populates="reviews")
