from __future__ import annotations

import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import current_user
from app.core.db import get_db
from app.models import Deck, Folder, User
from app.services.access import can_manage_deck, can_manage_decks

router = APIRouter(prefix="/api/v1", tags=["folders"])

# Folder name validation pattern: only a-z, A-Z, 0-9, underscore
FOLDER_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")


class FolderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    parent_id: UUID | None = None
    organization_id: UUID | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not FOLDER_NAME_PATTERN.match(v):
            raise ValueError("Folder name can only contain letters, numbers, and underscores")
        return v


class FolderUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not FOLDER_NAME_PATTERN.match(v):
            raise ValueError("Folder name can only contain letters, numbers, and underscores")
        return v


class FolderResponse(BaseModel):
    id: UUID
    name: str
    parent_id: UUID | None
    user_id: UUID
    organization_id: UUID | None
    created_at: str
    updated_at: str
    deck_count: int = 0
    subfolder_count: int = 0

    class Config:
        from_attributes = True


class FolderBreadcrumb(BaseModel):
    id: UUID | None
    name: str


class MoveDecksRequest(BaseModel):
    deck_ids: list[UUID]
    folder_id: UUID | None = None  # None means move to root


def _get_folder_tree_path(db: Session, folder_id: UUID) -> list[FolderBreadcrumb]:
    """Get the path from root to the given folder for breadcrumb navigation."""
    path = []
    current_id: UUID | None = folder_id
    while current_id:
        folder = db.get(Folder, current_id)
        if not folder:
            break
        path.insert(0, FolderBreadcrumb(id=folder.id, name=folder.name))
        current_id = folder.parent_id
    return path


def _count_decks_in_folder(db: Session, folder_id: UUID) -> int:
    """Count decks directly in this folder (not subfolders)."""
    return db.execute(
        select(func.count(Deck.id)).where(Deck.folder_id == folder_id, Deck.is_deleted.is_(False))
    ).scalar_one()


def _count_subfolders(db: Session, folder_id: UUID) -> int:
    """Count direct subfolders."""
    return db.execute(
        select(func.count(Folder.id)).where(Folder.parent_id == folder_id)
    ).scalar_one()


def _folder_to_response(db: Session, folder: Folder) -> FolderResponse:
    """Convert Folder model to response with counts."""
    return FolderResponse(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        user_id=folder.user_id,
        organization_id=folder.organization_id,
        created_at=folder.created_at.isoformat(),
        updated_at=folder.updated_at.isoformat(),
        deck_count=_count_decks_in_folder(db, folder.id),
        subfolder_count=_count_subfolders(db, folder.id),
    )


@router.get("/folders", response_model=list[FolderResponse])
def list_root_folders(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """List root folders (parent_id=null) for the current user/org."""
    folders = db.execute(
        select(Folder)
        .where(
            Folder.parent_id.is_(None),
            Folder.user_id == user.id,
        )
        .order_by(Folder.name.asc())
    ).scalars().all()

    return [_folder_to_response(db, f) for f in folders]


@router.get("/folders/{folder_id}", response_model=FolderResponse)
def get_folder(
    folder_id: UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Get a specific folder by ID."""
    folder = db.get(Folder, folder_id)
    if not folder or folder.user_id != user.id:
        raise HTTPException(status_code=404, detail="Folder not found")
    return _folder_to_response(db, folder)


@router.get("/folders/{folder_id}/subfolders", response_model=list[FolderResponse])
def list_subfolders(
    folder_id: UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """List direct subfolders of a folder."""
    parent = db.get(Folder, folder_id)
    if not parent or parent.user_id != user.id:
        raise HTTPException(status_code=404, detail="Folder not found")

    folders = db.execute(
        select(Folder)
        .where(Folder.parent_id == folder_id)
        .order_by(Folder.name.asc())
    ).scalars().all()

    return [_folder_to_response(db, f) for f in folders]


@router.get("/folders/{folder_id}/breadcrumb", response_model=list[FolderBreadcrumb])
def get_folder_breadcrumb(
    folder_id: UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Get breadcrumb path for a folder."""
    folder = db.get(Folder, folder_id)
    if not folder or folder.user_id != user.id:
        raise HTTPException(status_code=404, detail="Folder not found")
    return _get_folder_tree_path(db, folder_id)


@router.post("/folders", response_model=FolderResponse, status_code=201)
def create_folder(
    folder_data: FolderCreate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Create a new folder."""
    if not can_manage_decks(user):
        raise HTTPException(status_code=403, detail="You do not have permission to create folders")

    # Validate parent folder if provided
    parent_folder = None
    if folder_data.parent_id:
        parent_folder = db.get(Folder, folder_data.parent_id)
        if not parent_folder or parent_folder.user_id != user.id:
            raise HTTPException(status_code=404, detail="Parent folder not found")

    folder = Folder(
        name=folder_data.name,
        parent_id=folder_data.parent_id,
        user_id=user.id,
        organization_id=user.organization_id,
    )

    try:
        db.add(folder)
        db.commit()
        db.refresh(folder)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A folder with this name already exists in this location")

    return _folder_to_response(db, folder)


@router.put("/folders/{folder_id}", response_model=FolderResponse)
def update_folder(
    folder_id: UUID,
    folder_data: FolderUpdate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Rename a folder."""
    folder = db.get(Folder, folder_id)
    if not folder or folder.user_id != user.id:
        raise HTTPException(status_code=404, detail="Folder not found")

    folder.name = folder_data.name

    try:
        db.commit()
        db.refresh(folder)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A folder with this name already exists in this location")

    return _folder_to_response(db, folder)


@router.delete("/folders/{folder_id}", status_code=204)
def delete_folder(
    folder_id: UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Delete a folder. Child decks are moved to parent folder (or root if root folder)."""
    folder = db.get(Folder, folder_id)
    if not folder or folder.user_id != user.id:
        raise HTTPException(status_code=404, detail="Folder not found")

    # Move child decks to parent folder (or root if this is root folder)
    db.execute(
        Deck.__table__.update()
        .where(Deck.folder_id == folder_id)
        .values(folder_id=folder.parent_id)
    )

    # Delete the folder (cascade will handle subfolders)
    db.delete(folder)
    db.commit()


@router.get("/folders/{folder_id}/decks", response_model=list[dict])
def list_folder_decks(
    folder_id: UUID,
    include_subfolders: bool = False,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """List decks in a folder, optionally including decks from subfolders."""
    folder = db.get(Folder, folder_id)
    if not folder or folder.user_id != user.id:
        raise HTTPException(status_code=404, detail="Folder not found")

    if include_subfolders:
        # Get all descendant folder IDs
        descendant_ids = [folder_id]
        to_process = [folder_id]
        while to_process:
            current = to_process.pop()
            children = db.execute(
                select(Folder.id).where(Folder.parent_id == current)
            ).scalars().all()
            for child_id in children:
                descendant_ids.append(child_id)
                to_process.append(child_id)

        decks = db.execute(
            select(Deck)
            .where(Deck.folder_id.in_(descendant_ids), Deck.is_deleted.is_(False))
            .order_by(Deck.name.asc())
        ).scalars().all()
    else:
        decks = db.execute(
            select(Deck)
            .where(Deck.folder_id == folder_id, Deck.is_deleted.is_(False))
            .order_by(Deck.name.asc())
        ).scalars().all()

    return [
        {
            "id": str(deck.id),
            "name": deck.name,
            "description": deck.description,
            "is_global": deck.is_global,
            "folder_id": str(deck.folder_id) if deck.folder_id else None,
        }
        for deck in decks
    ]


@router.post("/decks/move", status_code=200)
def move_decks(
    request: MoveDecksRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Move multiple decks to a folder. If folder_id is null, move to root."""
    if not can_manage_decks(user):
        raise HTTPException(status_code=403, detail="You do not have permission to move decks")

    if not request.deck_ids:
        raise HTTPException(status_code=400, detail="No deck IDs provided")

    # Validate target folder if provided
    if request.folder_id:
        target_folder = db.get(Folder, request.folder_id)
        if not target_folder or target_folder.user_id != user.id:
            raise HTTPException(status_code=404, detail="Target folder not found")

    # Validate user can manage all decks
    managed_count = 0
    for deck_id in request.deck_ids:
        deck = db.get(Deck, deck_id)
        if deck and can_manage_deck(user, deck):
            managed_count += 1

    if managed_count != len(request.deck_ids):
        raise HTTPException(status_code=403, detail="You do not have permission to move one or more of these decks")

    # Move decks
    db.execute(
        Deck.__table__.update()
        .where(Deck.id.in_(request.deck_ids))
        .values(folder_id=request.folder_id)
    )
    db.commit()

    return {"success": True, "moved_count": len(request.deck_ids)}


class FolderMoveRequest(BaseModel):
    parent_id: UUID | None = None


@router.put("/folders/{folder_id}/move", status_code=200)
def move_folder(
    folder_id: UUID,
    request: FolderMoveRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Move a folder to a new parent (or root if parent_id is null)."""
    folder = db.get(Folder, folder_id)
    if not folder or folder.user_id != user.id:
        raise HTTPException(status_code=404, detail="Folder not found")

    # Prevent moving into itself or a descendant
    if request.parent_id:
        descendant_ids = set()
        def collect_descendants(pid: UUID):
            descendants = db.execute(
                select(Folder.id).where(Folder.parent_id == pid)
            ).scalars().all()
            for d in descendants:
                descendant_ids.add(d)
                collect_descendants(d)
        collect_descendants(folder_id)
        if request.parent_id in descendant_ids or request.parent_id == folder_id:
            raise HTTPException(status_code=400, detail="Cannot move a folder into itself or its descendant")

    # Check duplicate sibling name
    existing = db.execute(
        select(Folder).where(
            Folder.parent_id == request.parent_id,
            Folder.user_id == user.id,
            Folder.name == folder.name,
            Folder.id != folder_id,
        )
    ).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="A folder with this name already exists in the target location")

    folder.parent_id = request.parent_id
    db.commit()
    return {"success": True}


@router.get("/folders/tree", response_model=list[dict])
def get_folder_tree(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Get folder tree structure for folder picker."""
    def build_tree(parent_id: UUID | None) -> list[dict]:
        folders = db.execute(
            select(Folder)
            .where(Folder.parent_id == parent_id, Folder.user_id == user.id)
            .order_by(Folder.name.asc())
        ).scalars().all()

        result = []
        for folder in folders:
            result.append({
                "id": str(folder.id),
                "name": folder.name,
                "children": build_tree(folder.id),
            })
        return result

    return build_tree(None)
