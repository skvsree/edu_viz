# Feature Branch: feat/user-organization-analytics

## Overview
This feature branch adds comprehensive analytics visualization capabilities to edu_viz for:
1. **Per-user analytics** - Individual learning metrics within organization context
2. **Organization-level analytics** - Aggregated metrics for organization administrators  
3. **Global system analytics** - Platform-wide metrics for system administrators

## Components Created

### Database Models (`app/models/analytics.py`)
- `UserAnalytics`: Per-user study metrics aggregated by time period
- `OrganizationAnalytics`: Organization-wide aggregated metrics
- `SystemAnalytics`: Global platform metrics
- `AnalyticsEvent`: Raw event tracking for detailed analysis
- `AnalyticsEventType`: Enum for different types of trackable events

### API Endpoints (`app/api/routers/analytics.py`)
- `GET /api/v1/analytics/user/{user_id}` - User-specific analytics (with permission checks)
- `GET /api/v1/analytics/organization/{org_id}` - Organization analytics (admin+ only)
- `GET /api/v1/analytics/system` - System-wide analytics (system admin only)
- `GET /api/v1/analytics/events` - Raw analytics events (filtered by permissions)

### Services (`app/services/analytics.py`)
- `AnalyticsService`: Core service for tracking events and updating analytics
- Event tracking with automatic metric updates
- Period-based analytics aggregation
- Data cleanup utilities

### Schemas (`app/schemas/analytics.py`)
- Pydantic models for request/response validation
- ORM mode enabled for easy model conversion
- Comprehensive field validation and documentation

### Database Migration (`alembic/versions/0009_add_analytics_tables.py`)
- Creates all four analytics tables with appropriate indexes
- Includes enum type for analytics event types
- Proper foreign key relationships to users and organizations

### Main Application Integration (`app/main.py`)
- Added analytics router with `/api/v1/analytics` prefix
- Maintains existing middleware and routing structure

## Key Features

### Permission-Based Access Control
- **Users**: Can only view their own analytics
- **Organization Admins**: Can view analytics for users in their organization
- **System Administrators**: Can view all analytics (user, org, system)

### Metrics Tracked
**User Level**:
- Review counts, accuracy rates, study time
- Content engagement (decks/cards studied)
- Test performance and AI-assisted learning metrics

**Organization Level**:
- User engagement (total/active users)
- Aggregated review and test metrics
- Content utilization statistics

**System Level**:
- Platform adoption (organizations, users)
- Overall learning effectiveness
- AI-generated content usage

### Event Tracking
Tracks key learning activities:
- Review completions (with correctness ratings)
- Test starts/completions
- Deck and card creation
- AI-assisted content processing
- MCQ generation and practice
- Tag assignments

## Implementation Notes

### Extensibility
- New event types can be added to `AnalyticsEventType` enum
- Additional metrics can be added to analytics models
- Service methods can be extended for specialized analytics

### Performance Considerations
- Analytics are aggregated by time periods (default 30 days)
- Indexes on frequently queried columns (user_id, period dates, event_type)
- Raw events can be cleaned up periodically to prevent bloat

### Integration Points
The analytics service is designed to be called from existing services:
- `review_service.py` - Track review completions
- `tests.py` - Track test starts/completions
- `ai_generation.py` - Track AI upload processing
- `content.py` - Track deck/card creation and MCQ generation

## Next Steps for Implementation in Main Repo

1. **Apply the migration**: `alembic upgrade head`
2. **Integrate service calls**: Add analytics tracking to existing services
3. **Create frontend components**: Dashboard widgets for displaying analytics
4. **Add API documentation**: Update OpenAPI specs with new endpoints
5. **Create admin interfaces**: Organization and system admin analytics views

## Files Created
```
edu_viz-feature/
├── README.md
├── IMPLEMENTATION_SUMMARY.md
├── app/
│   ├── main.py
│   ├── models/
│   │   └── analytics.py
│   ├── api/
│   │   └── routers/
│   │       └── analytics.py
│   ├── services/
│   │   └── analytics.py
│   └── schemas/
│       └── analytics.py
└── alembic/
    └── versions/
        └── 0009_add_analytics_tables.py
```