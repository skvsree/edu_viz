from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field
from uuid import UUID

from app.models.analytics import AnalyticsEventType


class AnalyticsEventBase(BaseModel):
    user_id: Optional[UUID] = None
    organization_id: Optional[UUID] = None
    event_type: AnalyticsEventType
    event_timestamp: datetime
    event_data: Optional[str] = None
    source_ip: Optional[str] = None
    user_agent: Optional[str] = None


class AnalyticsEventCreate(AnalyticsEventBase):
    pass


class AnalyticsEventResponse(AnalyticsEventBase):
    id: UUID
    created_at: datetime

    class Config:
        orm_mode = True


class UserAnalyticsBase(BaseModel):
    user_id: UUID
    period_start: datetime
    period_end: datetime
    total_reviews: int = Field(default=0, ge=0)
    correct_reviews: int = Field(default=0, ge=0)
    accuracy_rate: float = Field(default=0.0, ge=0, le=1)
    total_study_time_minutes: int = Field(default=0, ge=0)
    average_session_minutes: float = Field(default=0.0, ge=0)
    study_streak_days: int = Field(default=0, ge=0)
    decks_studied: int = Field(default=0, ge=0)
    cards_studied: int = Field(default=0, ge=0)
    new_cards_learned: int = Field(default=0, ge=0)
    tests_taken: int = Field(default=0, ge=0)
    average_test_score: float = Field(default=0.0, ge=0, le=100)
    ai_generated_cards_studied: int = Field(default=0, ge=0)
    mcqs_practiced: int = Field(default=0, ge=0)


class UserAnalyticsCreate(UserAnalyticsBase):
    pass


class UserAnalyticsResponse(UserAnalyticsBase):
    id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


class OrganizationAnalyticsBase(BaseModel):
    organization_id: UUID
    period_start: datetime
    period_end: datetime
    total_users: int = Field(default=0, ge=0)
    active_users: int = Field(default=0, ge=0)
    total_reviews: int = Field(default=0, ge=0)
    correct_reviews: int = Field(default=0, ge=0)
    organization_accuracy_rate: float = Field(default=0.0, ge=0, le=1)
    total_study_time_minutes: int = Field(default=0, ge=0)
    average_study_time_per_user: float = Field(default=0.0, ge=0)
    total_decks: int = Field(default=0, ge=0)
    total_cards: int = Field(default=0, ge=0)
    decks_studied: int = Field(default=0, ge=0)
    tests_taken: int = Field(default=0, ge=0)
    average_test_score: float = Field(default=0.0, ge=0, le=100)
    ai_generated_content_used: int = Field(default=0, ge=0)


class OrganizationAnalyticsCreate(OrganizationAnalyticsBase):
    pass


class OrganizationAnalyticsResponse(OrganizationAnalyticsBase):
    id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


class SystemAnalyticsBase(BaseModel):
    period_start: datetime
    period_end: datetime
    total_organizations: int = Field(default=0, ge=0)
    total_users: int = Field(default=0, ge=0)
    active_users: int = Field(default=0, ge=0)
    system_admins: int = Field(default=0, ge=0)
    total_reviews: int = Field(default=0, ge=0)
    correct_reviews: int = Field(default=0, ge=0)
    system_accuracy_rate: float = Field(default=0.0, ge=0, le=1)
    total_study_time_minutes: int = Field(default=0, ge=0)
    average_study_time_per_user: float = Field(default=0.0, ge=0)
    total_decks: int = Field(default=0, ge=0)
    total_cards: int = Field(default=0, ge=0)
    total_ai_generated_cards: int = Field(default=0, ge=0)
    total_mcqs_generated: int = Field(default=0, ge=0)
    tests_taken: int = Field(default=0, ge=0)
    average_test_score: float = Field(default=0.0, ge=0, le=100)


class SystemAnalyticsCreate(SystemAnalyticsBase):
    pass


class SystemAnalyticsResponse(SystemAnalyticsBase):
    id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True