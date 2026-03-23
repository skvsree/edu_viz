from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics import (
    AnalyticsEvent,
    AnalyticsEventType,
    OrganizationAnalytics,
    SystemAnalytics,
    UserAnalytics,
)
from app.models.user import User
from app.models.organization import Organization


class AnalyticsService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def track_event(
        self,
        event_type: AnalyticsEventType,
        user_id: Optional[uuid.UUID] = None,
        organization_id: Optional[uuid.UUID] = None,
        event_data: Optional[dict] = None,
        source_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> AnalyticsEvent:
        """
        Track an analytics event.
        """
        event = AnalyticsEvent(
            user_id=user_id,
            organization_id=organization_id,
            event_type=event_type,
            event_data=json.dumps(event_data) if event_data else None,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        self.db.add(event)
        await self.db.flush()
        await self.db.refresh(event)
        return event

    async def get_user_analytics(
        self, user_id: uuid.UUID, period_days: int = 30
    ) -> UserAnalytics:
        """
        Get or create user analytics for the specified time period.
        """
        period_end = datetime.utcnow()
        period_start = period_end - timedelta(days=period_days)

        # Try to get existing analytics
        result = await self.db.execute(
            select(UserAnalytics).where(
                and_(
                    UserAnalytics.user_id == user_id,
                    UserAnalytics.period_start == period_start,
                    UserAnalytics.period_end == period_end,
                )
            )
        )
        user_analytics = result.scalar_one_or_none()

        if not user_analytics:
            # Create new analytics record
            user_analytics = UserAnalytics(
                user_id=user_id,
                period_start=period_start,
                period_end=period_end,
            )
            self.db.add(user_analytics)
            await self.db.flush()
            await self.db.refresh(user_analytics)

        return user_analytics

    async def update_user_analytics_from_event(
        self, event: AnalyticsEvent
    ) -> None:
        """
        Update user analytics based on a tracked event.
        This would typically be called asynchronously after tracking an event.
        """
        if not event.user_id:
            return

        # Get the user's analytics for today (or appropriate period)
        user_analytics = await self.get_user_analytics(event.user_id, period_days=1)
        
        # Update metrics based on event type
        if event.event_type == AnalyticsEventType.REVIEW_COMPLETED:
            if event.event_data:
                data = json.loads(event.event_data)
                user_analytics.total_reviews += 1
                if data.get("rating", 0) >= 3:  # Correct if rating 3 or 4
                    user_analytics.correct_reviews += 1
                user_analytics.accuracy_rate = (
                    user_analytics.correct_reviews / user_analytics.total_reviews
                    if user_analytics.total_reviews > 0
                    else 0.0
                )
                user_analytics.total_study_time_minutes += data.get(
                    "study_time_seconds", 0
                ) // 60

        elif event.event_type == AnalyticsEventType.TEST_COMPLETED:
            if event.event_data:
                data = json.loads(event.event_data)
                user_analytics.tests_taken += 1
                # Update average test score
                current_total = user_analytics.average_test_score * (
                    user_analytics.tests_taken - 1
                )
                user_analytics.average_test_score = (
                    current_total + data.get("score", 0)
                ) / user_analytics.tests_taken

        elif event.event_type == AnalyticsEventType.AI_UPLOAD_PROCESSED:
            if event.event_data:
                data = json.loads(event.event_data)
                user_analytics.ai_generated_cards_studied += data.get(
                    "cards_created", 0
                )

        elif event.event_type == AnalyticsEventType.MCQ_GENERATED:
            if event.event_data:
                data = json.loads(event.event_data)
                user_analytics.mcqs_practiced += data.get("mcqs_generated", 0)

        await self.db.flush()

    async def get_organization_analytics(
        self, organization_id: uuid.UUID, period_days: int = 30
    ) -> OrganizationAnalytics:
        """
        Get or create organization analytics for the specified time period.
        """
        period_end = datetime.utcnow()
        period_start = period_end - timedelta(days=period_days)

        # Try to get existing analytics
        result = await self.db.execute(
            select(OrganizationAnalytics).where(
                and_(
                    OrganizationAnalytics.organization_id == organization_id,
                    OrganizationAnalytics.period_start == period_start,
                    OrganizationAnalytics.period_end == period_end,
                )
            )
        )
        org_analytics = result.scalar_one_or_none()

        if not org_analytics:
            # Create new analytics record
            org_analytics = OrganizationAnalytics(
                organization_id=organization_id,
                period_start=period_start,
                period_end=period_end,
            )
            self.db.add(org_analytics)
            await self.db.flush()
            await self.db.refresh(org_analytics)

        return org_analytics

    async def get_system_analytics(
        self, period_days: int = 30
    ) -> SystemAnalytics:
        """
        Get or create system analytics for the specified time period.
        """
        period_end = datetime.utcnow()
        period_start = period_end - timedelta(days=period_days)

        # Try to get existing analytics
        result = await self.db.execute(
            select(SystemAnalytics).where(
                and_(
                    SystemAnalytics.period_start == period_start,
                    SystemAnalytics.period_end == period_end,
                )
            )
        )
        system_analytics = result.scalar_one_or_none()

        if not system_analytics:
            # Create new analytics record
            system_analytics = SystemAnalytics(
                period_start=period_start,
                period_end=period_end,
            )
            self.db.add(system_analytics)
            await self.db.flush()
            await self.db.refresh(system_analytics)

        return system_analytics

    async def get_analytics_events(
        self,
        user_id: Optional[uuid.UUID] = None,
        organization_id: Optional[uuid.UUID] = None,
        event_type: Optional[AnalyticsEventType] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[AnalyticsEvent]:
        """
        Get analytics events with filtering.
        """
        query = select(AnalyticsEvent)

        if user_id:
            query = query.where(AnalyticsEvent.user_id == user_id)
        if organization_id:
            query = query.where(AnalyticsEvent.organization_id == organization_id)
        if event_type:
            query = query.where(AnalyticsEvent.event_type == event_type)

        query = (
            query.order_by(AnalyticsEvent.event_timestamp.desc())
            .offset(offset)
            .limit(limit)
        )

        result = await self.db.execute(query)
        return result.scalars().all()

    async def cleanup_old_analytics(self, days_to_keep: int = 365) -> int:
        """
        Clean up old analytics events to prevent database bloat.
        Returns number of rows deleted.
        """
        cutoff_date = datetime.utcnow() - timedelta(days=days_to_keep)
        
        # Delete old events
        result = await self.db.execute(
            delete(AnalyticsEvent).where(
                AnalyticsEvent.event_timestamp < cutoff_date
            )
        )
        deleted_count = result.rowcount
        
        await self.db.flush()
        return deleted_count