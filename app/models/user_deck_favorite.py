import uuid

from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class UserDeckFavorite(Base):
    __tablename__ = "user_deck_favorites"

    __table_args__ = (
        UniqueConstraint("user_id", "deck_id", name="ux_user_deck_favorites_user_deck"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    deck_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    # NOTE: we keep timestamps out for now; favorites are toggled frequently and
    # can be derived from created_at if later needed.
