import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.api.routers.pages import templates
from app.core.db import get_db
from app.models import AnalyticsEvent, AnalyticsEventType, OrganizationAnalytics, SystemAnalytics, User, UserAnalytics

router = APIRouter(tags=["analytics"])


def _parse_uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {label} format") from exc


@router.get("/api/v1/analytics/user/{user_id}")
def get_user_analytics_api(
    user_id: str,
    period_days: int = Query(30, ge=1, le=365),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    target_user_id = _parse_uuid(user_id, "user ID")

    if target_user_id != user.id:
        if user.role == "admin" and user.organization_id:
            target_user = db.get(User, target_user_id)
            if not target_user or target_user.organization_id != user.organization_id:
                raise HTTPException(status_code=403, detail="Not authorized to view this user's analytics")
        elif user.role != "system_admin":
            raise HTTPException(status_code=403, detail="Not authorized to view this user's analytics")

    period_end = datetime.utcnow()
    period_start = period_end - timedelta(days=period_days)

    record = db.execute(
        select(UserAnalytics).where(UserAnalytics.user_id == target_user_id).order_by(UserAnalytics.period_end.desc())
    ).scalars().first()

    if record is None:
        return {
            "user_id": str(target_user_id),
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "total_reviews": 0,
            "correct_reviews": 0,
            "accuracy_rate": 0.0,
            "total_study_time_minutes": 0,
            "average_session_minutes": 0.0,
            "study_streak_days": 0,
            "decks_studied": 0,
            "cards_studied": 0,
            "new_cards_learned": 0,
            "tests_taken": 0,
            "average_test_score": 0.0,
            "ai_generated_cards_studied": 0,
            "mcqs_practiced": 0,
        }

    return {
        "id": str(record.id),
        "user_id": str(record.user_id),
        "period_start": record.period_start.isoformat(),
        "period_end": record.period_end.isoformat(),
        "total_reviews": record.total_reviews,
        "correct_reviews": record.correct_reviews,
        "accuracy_rate": record.accuracy_rate,
        "total_study_time_minutes": record.total_study_time_minutes,
        "average_session_minutes": record.average_session_minutes,
        "study_streak_days": record.study_streak_days,
        "decks_studied": record.decks_studied,
        "cards_studied": record.cards_studied,
        "new_cards_learned": record.new_cards_learned,
        "tests_taken": record.tests_taken,
        "average_test_score": record.average_test_score,
        "ai_generated_cards_studied": record.ai_generated_cards_studied,
        "mcqs_practiced": record.mcqs_practiced,
    }


@router.get("/api/v1/analytics/organization/{org_id}")
def get_organization_analytics_api(
    org_id: str,
    period_days: int = Query(30, ge=1, le=365),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    target_org_id = _parse_uuid(org_id, "organization ID")

    if user.role != "system_admin":
        if user.role != "admin" or user.organization_id != target_org_id:
            raise HTTPException(status_code=403, detail="Not authorized to view this organization's analytics")

    period_end = datetime.utcnow()
    period_start = period_end - timedelta(days=period_days)

    record = db.execute(
        select(OrganizationAnalytics)
        .where(OrganizationAnalytics.organization_id == target_org_id)
        .order_by(OrganizationAnalytics.period_end.desc())
    ).scalars().first()

    if record is None:
        return {
            "organization_id": str(target_org_id),
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "total_users": 0,
            "active_users": 0,
            "total_reviews": 0,
            "correct_reviews": 0,
            "organization_accuracy_rate": 0.0,
            "total_study_time_minutes": 0,
            "average_study_time_per_user": 0.0,
            "total_decks": 0,
            "total_cards": 0,
            "decks_studied": 0,
            "tests_taken": 0,
            "average_test_score": 0.0,
            "ai_generated_content_used": 0,
        }

    return {
        "id": str(record.id),
        "organization_id": str(record.organization_id),
        "period_start": record.period_start.isoformat(),
        "period_end": record.period_end.isoformat(),
        "total_users": record.total_users,
        "active_users": record.active_users,
        "total_reviews": record.total_reviews,
        "correct_reviews": record.correct_reviews,
        "organization_accuracy_rate": record.organization_accuracy_rate,
        "total_study_time_minutes": record.total_study_time_minutes,
        "average_study_time_per_user": record.average_study_time_per_user,
        "total_decks": record.total_decks,
        "total_cards": record.total_cards,
        "decks_studied": record.decks_studied,
        "tests_taken": record.tests_taken,
        "average_test_score": record.average_test_score,
        "ai_generated_content_used": record.ai_generated_content_used,
    }


@router.get("/api/v1/analytics/system")
def get_system_analytics_api(
    period_days: int = Query(30, ge=1, le=365),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if user.role != "system_admin":
        raise HTTPException(status_code=403, detail="Only system administrators can view system analytics")

    period_end = datetime.utcnow()
    period_start = period_end - timedelta(days=period_days)

    record = db.execute(select(SystemAnalytics).order_by(SystemAnalytics.period_end.desc())).scalars().first()

    if record is None:
        return {
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "total_organizations": 0,
            "total_users": 0,
            "active_users": 0,
            "system_admins": 0,
            "total_reviews": 0,
            "correct_reviews": 0,
            "system_accuracy_rate": 0.0,
            "total_study_time_minutes": 0,
            "average_study_time_per_user": 0.0,
            "total_decks": 0,
            "total_cards": 0,
            "total_ai_generated_cards": 0,
            "total_mcqs_generated": 0,
            "tests_taken": 0,
            "average_test_score": 0.0,
        }

    return {
        "id": str(record.id),
        "period_start": record.period_start.isoformat(),
        "period_end": record.period_end.isoformat(),
        "total_organizations": record.total_organizations,
        "total_users": record.total_users,
        "active_users": record.active_users,
        "system_admins": record.system_admins,
        "total_reviews": record.total_reviews,
        "correct_reviews": record.correct_reviews,
        "system_accuracy_rate": record.system_accuracy_rate,
        "total_study_time_minutes": record.total_study_time_minutes,
        "average_study_time_per_user": record.average_study_time_per_user,
        "total_decks": record.total_decks,
        "total_cards": record.total_cards,
        "total_ai_generated_cards": record.total_ai_generated_cards,
        "total_mcqs_generated": record.total_mcqs_generated,
        "tests_taken": record.tests_taken,
        "average_test_score": record.average_test_score,
    }


@router.get("/analytics", response_class=HTMLResponse)
def analytics_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    user_record = db.execute(
        select(UserAnalytics).where(UserAnalytics.user_id == user.id).order_by(UserAnalytics.period_end.desc())
    ).scalars().first()

    org_record = None
    if user.organization_id and user.role in {"admin", "system_admin"}:
        org_record = db.execute(
            select(OrganizationAnalytics)
            .where(OrganizationAnalytics.organization_id == user.organization_id)
            .order_by(OrganizationAnalytics.period_end.desc())
        ).scalars().first()

    system_record = None
    if user.role == "system_admin":
        system_record = db.execute(select(SystemAnalytics).order_by(SystemAnalytics.period_end.desc())).scalars().first()

    recent_events_stmt = select(AnalyticsEvent)
    if user.role == "system_admin":
        pass
    elif user.role == "admin" and user.organization_id:
        recent_events_stmt = recent_events_stmt.where(AnalyticsEvent.organization_id == user.organization_id)
    else:
        recent_events_stmt = recent_events_stmt.where(AnalyticsEvent.user_id == user.id)
    recent_events = db.execute(recent_events_stmt.order_by(AnalyticsEvent.event_timestamp.desc()).limit(20)).scalars().all()

    return templates.TemplateResponse(
        "analytics/index.html",
        {
            "request": request,
            "user": user,
            "user_analytics": user_record,
            "organization_analytics": org_record,
            "system_analytics": system_record,
            "recent_events": recent_events,
            "title": "Analytics | edu selviz",
        },
    )