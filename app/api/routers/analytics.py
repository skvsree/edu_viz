from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_user
from app.core.db import get_async_session
from app.models.analytics import (
    AnalyticsEvent,
    AnalyticsEventType,
    OrganizationAnalytics,
    SystemAnalytics,
    UserAnalytics,
)
from app.models.user import User
from app.models.organization import Organization
from app.schemas.analytics import (
    AnalyticsEventResponse,
    OrganizationAnalyticsResponse,
    SystemAnalyticsResponse,
    UserAnalyticsResponse,
)

router = APIRouter()


@router.get("/user/{user_id}", response_model=UserAnalyticsResponse)
async def get_user_analytics(
    user_id: str,
    period_days: int = Query(30, ge=1, le=365, description="Number of days to look back"),
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session),
):
    """
    Get analytics for a specific user.
    Users can only view their own analytics unless they are org admins or system admins.
    """
    # Parse user_id
    try:
        target_user_id = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user ID format",
        )

    # Permission check: users can view their own data, org admins can view org members,
    # system admins can view anyone
    if target_user_id != current_user.id:
        # Check if current user is org admin and target user is in same org
        if current_user.role == "admin" and current_user.organization_id:
            # Verify target user is in same organization
            target_user_result = await session.execute(
                select(User).where(User.id == target_user_id)
            )
            target_user = target_user_result.scalar_one_or_none()
            
            if not target_user or target_user.organization_id != current_user.organization_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not authorized to view this user's analytics",
                )
        elif current_user.role != "system_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to view this user's analytics",
            )

    # Calculate time period
    period_end = datetime.utcnow()
    period_start = period_end - timedelta(days=period_days)

    # Get or create user analytics record
    result = await session.execute(
        select(UserAnalytics).where(
            and_(
                UserAnalytics.user_id == target_user_id,
                UserAnalytics.period_start == period_start,
                UserAnalytics.period_end == period_end,
            )
        )
    )
    user_analytics = result.scalar_one_or_none()

    if not user_analytics:
        # Return empty analytics if no data exists for the period
        user_analytics = UserAnalytics(
            user_id=target_user_id,
            period_start=period_start,
            period_end=period_end,
        )

    return user_analytics


@router.get("/organization/{org_id}", response_model=OrganizationAnalyticsResponse)
async def get_organization_analytics(
    org_id: str,
    period_days: int = Query(30, ge=1, le=365, description="Number of days to look back"),
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session),
):
    """
    Get analytics for an organization.
    Only organization admins and system admins can view organization analytics.
    """
    # Parse org_id
    try:
        target_org_id = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid organization ID format",
        )

    # Permission check: only org admins (of that org) and system admins can view org analytics
    if current_user.role != "system_admin":
        if current_user.role != "admin" or current_user.organization_id != target_org_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to view this organization's analytics",
            )

    # Calculate time period
    period_end = datetime.utcnow()
    period_start = period_end - timedelta(days=period_days)

    # Get or create organization analytics record
    result = await session.execute(
        select(OrganizationAnalytics).where(
            and_(
                OrganizationAnalytics.organization_id == target_org_id,
                OrganizationAnalytics.period_start == period_start,
                OrganizationAnalytics.period_end == period_end,
            )
        )
    )
    org_analytics = result.scalar_one_or_none()

    if not org_analytics:
        # Return empty analytics if no data exists for the period
        org_analytics = OrganizationAnalytics(
            organization_id=target_org_id,
            period_start=period_start,
            period_end=period_end,
        )

    return org_analytics


@router.get("/system", response_model=SystemAnalyticsResponse)
async def get_system_analytics(
    period_days: int = Query(30, ge=1, le=365, description="Number of days to look back"),
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session),
):
    """
    Get system-wide analytics.
    Only system administrators can view system analytics.
    """
    if current_user.role != "system_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only system administrators can view system analytics",
        )

    # Calculate time period
    period_end = datetime.utcnow()
    period_start = period_end - timedelta(days=period_days)

    # Get or create system analytics record
    result = await session.execute(
        select(SystemAnalytics).where(
            and_(
                SystemAnalytics.period_start == period_start,
                SystemAnalytics.period_end == period_end,
            )
        )
    )
    system_analytics = result.scalar_one_or_none()

    if not system_analytics:
        # Return empty analytics if no data exists for the period
        system_analytics = SystemAnalytics(
            period_start=period_start,
            period_end=period_end,
        )

    return system_analytics


@router.get("/events", response_model=List[AnalyticsEventResponse])
async def get_analytics_events(
    event_type: Optional[AnalyticsEventType] = Query(None, description="Filter by event type"),
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    organization_id: Optional[str] = Query(None, description="Filter by organization ID"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of events to return"),
    offset: int = Query(0, ge=0, description="Number of events to skip"),
    current_user: User = Depends(get_current_active_user),
    session: AsyncSession = Depends(get_async_session),
):
    """
    Get raw analytics events with filtering.
    Access is restricted based on user role and permissions.
    """
    # Build base query
    query = select(AnalyticsEvent)

    # Apply permission-based filters
    if current_user.role != "system_admin":
        if current_user.role == "admin" and current_user.organization_id:
            # Org admins can see events from their organization
            query = query.where(AnalyticsEvent.organization_id == current_user.organization_id)
        else:
            # Regular users can only see their own events
            query = query.where(AnalyticsEvent.user_id == current_user.id)

    # Apply additional filters
    if event_type:
        query = query.where(AnalyticsEvent.event_type == event_type)
    
    if user_id:
        try:
            user_uuid = uuid.UUID(user_id)
            query = query.where(AnalyticsEvent.user_id == user_uuid)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid user ID format",
            )
    
    if organization_id:
        try:
            org_uuid = uuid.UUID(organization_id)
            query = query.where(AnalyticsEvent.organization_id == org_uuid)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid organization ID format",
            )

    # Apply pagination and ordering
    query = query.order_by(AnalyticsEvent.event_timestamp.desc()).offset(offset).limit(limit)

    # Execute query
    result = await session.execute(query)
    events = result.scalars().all()

    return events