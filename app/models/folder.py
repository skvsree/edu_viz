import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class Folder(Base):
    __tablename__ = "folders"
    __table_args__ = (
        # Unique constraint: no duplicate sibling folder names for same user
        UniqueConstraint(
            "parent_id",
            "user_id",
            "name",
            name="ux_folders_parent_user_name",
        ),
        # Unique constraint: no duplicate sibling folder names for same organization
        UniqueConstraint(
            "parent_id",
            "organization_id",
            "name",
            name="ux_folders_parent_org_name",
        ),
        Index(
            "ix_folders_parent_id",
            "parent_id",
        ),
        Index(
            "ix_folders_user_id",
            "user_id",
        ),
        Index(
            "ix_folders_organization_id",
            "organization_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("folders.id", ondelete="CASCADE"),
        nullable=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    # Self-referential relationship for nested folders
    parent: Mapped["Folder | None"] = relationship(
        "Folder",
        remote_side=[id],
        back_populates="children",
    )
    children: Mapped[list["Folder"]] = relationship(
        "Folder",
        back_populates="parent",
        cascade="all, delete-orphan",
    )
    # Relationship to decks
    decks: Mapped[list["Deck"]] = relationship(
        "Deck",
        back_populates="folder",
    )
