# Feedback API Specification

## Overview

This document defines the API contract for the platform feedback endpoint that allows users to submit feedback, bug reports, feature requests, and general comments about the Jawafdehi platform.

## Endpoint

```
POST /api/feedback/
```

## Authentication

- **Required**: No (public endpoint)
- **Rate Limiting**: Recommended - 5 submissions per IP per hour
- **CORS**: Same as other API endpoints.

## Request

### Headers

```
Content-Type: application/json
Accept: application/json
```

### Request Body Schema

```json
{
  "feedbackType": "string (required)",
  "subject": "string (required)",
  "description": "string (required)",
  "relatedPage": "string (optional)",
  "contactInfo": {
    "name": "string (optional)",
    "contactMethods": [
      {
        "type": "string (required)",
        "value": "string (required)"
      }
    ]
  }
}
```

### Field Specifications

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `feedbackType` | string | Yes | Enum: `bug`, `feature`, `usability`, `content`, `general` | Type of feedback |
| `subject` | string | Yes | Max 200 chars | Brief summary of feedback |
| `description` | string | Yes | Max 5000 chars | Detailed feedback description |
| `relatedPage` | string | No | Max 300 chars | Page or feature related to feedback |
| `contactInfo` | object | No | - | Optional contact information |
| `contactInfo.name` | string | No | Max 200 chars | Submitter's name |
| `contactInfo.contactMethods` | array | No | Max 5 items | List of contact methods |
| `contactInfo.contactMethods[].type` | string | Yes (if array present) | Enum: `email`, `phone`, `whatsapp`, `instagram`, `facebook`, `other` | Contact method type |
| `contactInfo.contactMethods[].value` | string | Yes (if array present) | Max 300 chars | Contact value (email, phone, username, etc.) |

### Example Request

```bash
curl -X POST "https://portal.jawafdehi.org/api/feedback/" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d '{
    "feedbackType": "bug",
    "subject": "Search not working on Cases page",
    "description": "When I try to search for cases using the search bar, nothing happens. Steps to reproduce: 1. Go to Cases page 2. Type '\''corruption'\'' in search 3. Press Enter 4. No results appear",
    "relatedPage": "Cases page - Search functionality",
    "contactInfo": {
      "name": "राम बहादुर",
      "contactMethods": [
        {
          "type": "email",
          "value": "ram.bahadur@example.com"
        },
        {
          "type": "whatsapp",
          "value": "+977 9812345678"
        }
      ]
    }
  }'
```

### Minimal Example (Anonymous Feedback)

```bash
curl -X POST "https://portal.jawafdehi.org/api/feedback/" \
  -H "Content-Type: application/json" \
  -d '{
    "feedbackType": "general",
    "subject": "Great platform!",
    "description": "This platform is very helpful for tracking corruption cases in Nepal."
  }'
```

## Response

### Success Response (201 Created)

```json
{
  "id": 42,
  "feedbackType": "bug",
  "subject": "Search not working on Cases page",
  "status": "submitted",
  "submittedAt": "2024-12-10T14:30:00Z",
  "message": "Thank you for your feedback! We will review it and get back to you if needed."
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Unique identifier for the feedback submission |
| `feedbackType` | string | Type of feedback submitted |
| `subject` | string | Subject of the feedback |
| `status` | string | Status of feedback (always "submitted" on creation) |
| `submittedAt` | string (ISO 8601) | Timestamp when feedback was submitted |
| `message` | string | Confirmation message for the user |

### Error Responses

#### 400 Bad Request

```json
{
  "error": "Validation error",
  "details": {
    "feedbackType": ["This field is required."],
    "subject": ["This field is required."],
    "description": ["This field is required."]
  }
}
```

#### 400 Bad Request - Invalid Feedback Type

```json
{
  "error": "Validation error",
  "details": {
    "feedbackType": ["Invalid feedback type. Must be one of: bug, feature, usability, content, general"]
  }
}
```

#### 400 Bad Request - Invalid Contact Method Type

```json
{
  "error": "Validation error",
  "details": {
    "contactInfo": {
      "contactMethods": [
        {
          "type": ["Invalid contact method type. Must be one of: email, phone, whatsapp, instagram, facebook, other"]
        }
      ]
    }
  }
}
```

#### 429 Too Many Requests

```json
{
  "error": "Rate limit exceeded",
  "detail": "Too many feedback submissions. Please try again later.",
  "retryAfter": 3600
}
```

#### 500 Internal Server Error

```json
{
  "error": "Unable to process feedback",
  "detail": "An unexpected error occurred. Please try again later."
}
```

## Database Model

### Feedback Model

```python
class FeedbackType(models.TextChoices):
    BUG = "bug", "Bug Report"
    FEATURE = "feature", "Feature Request"
    USABILITY = "usability", "Usability Issue"
    CONTENT = "content", "Content Feedback"
    GENERAL = "general", "General Feedback"


class FeedbackStatus(models.TextChoices):
    SUBMITTED = "submitted", "Submitted"
    IN_REVIEW = "in_review", "In Review"
    RESOLVED = "resolved", "Resolved"
    CLOSED = "closed", "Closed"


class Feedback(models.Model):
    """
    Platform feedback submissions from users.

    Stores feedback, bug reports, feature requests, and general comments
    about the Jawafdehi platform.
    """

    # Core fields
    feedback_type = models.CharField(
        max_length=20,
        choices=FeedbackType.choices,
        help_text="Type of feedback"
    )
    subject = models.CharField(
        max_length=200,
        help_text="Brief summary of feedback"
    )
    description = models.TextField(
        max_length=5000,
        help_text="Detailed feedback description"
    )
    related_page = models.CharField(
        max_length=300,
        blank=True,
        help_text="Page or feature related to feedback"
    )

    # Contact information (stored as JSON for flexibility)
    contact_info = models.JSONField(
        default=dict,
        blank=True,
        help_text="Optional contact information"
    )

    # Status tracking
    status = models.CharField(
        max_length=20,
        choices=FeedbackStatus.choices,
        default=FeedbackStatus.SUBMITTED,
        db_index=True,
        help_text="Current status of feedback"
    )

    # Metadata
    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text="IP address of submitter (for rate limiting)"
    )
    user_agent = models.TextField(
        blank=True,
        help_text="User agent string"
    )

    # Admin notes
    admin_notes = models.TextField(
        blank=True,
        help_text="Internal notes for administrators"
    )

    # Timestamps
    submitted_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-submitted_at']
        indexes = [
            models.Index(fields=['feedback_type', 'status']),
            models.Index(fields=['status', 'submitted_at']),
        ]

    def __str__(self):
        return f"{self.feedback_type.upper()}: {self.subject}"
```

### ContactInfo JSON Schema

```json
{
  "name": "string (optional)",
  "contactMethods": [
    {
      "type": "email|phone|whatsapp|instagram|facebook|other",
      "value": "string"
    }
  ]
}
```

## Implementation Notes

### Serializer

```python
from rest_framework import serializers
from .models import Feedback, FeedbackType


class ContactMethodSerializer(serializers.Serializer):
    type = serializers.ChoiceField(
        choices=['email', 'phone', 'whatsapp', 'instagram', 'facebook', 'other']
    )
    value = serializers.CharField(max_length=300)


class ContactInfoSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    contactMethods = ContactMethodSerializer(many=True, required=False)


class FeedbackSerializer(serializers.ModelSerializer):
    contactInfo = ContactInfoSerializer(required=False, source='contact_info')

    class Meta:
        model = Feedback
        fields = [
            'id', 'feedbackType', 'subject', 'description',
            'relatedPage', 'contactInfo', 'status', 'submittedAt'
        ]
        read_only_fields = ['id', 'status', 'submittedAt']

    def to_representation(self, instance):
        """Convert snake_case to camelCase for API response."""
        data = super().to_representation(instance)
        return {
            'id': data['id'],
            'feedbackType': data['feedbackType'],
            'subject': data['subject'],
            'status': data['status'],
            'submittedAt': data['submittedAt'],
            'message': 'Thank you for your feedback! We will review it and get back to you if needed.'
        }
```

### View

```python
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from .models import Feedback
from .serializers import FeedbackSerializer


class FeedbackRateThrottle(AnonRateThrottle):
    rate = '5/hour'


class FeedbackView(APIView):
    throttle_classes = [FeedbackRateThrottle]

    def post(self, request):
        serializer = FeedbackSerializer(data=request.data)

        if serializer.is_valid():
            # Capture metadata
            feedback = serializer.save(
                ip_address=self.get_client_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', '')
            )

            return Response(
                serializer.to_representation(feedback),
                status=status.HTTP_201_CREATED
            )

        return Response(
            {
                'error': 'Validation error',
                'details': serializer.errors
            },
            status=status.HTTP_400_BAD_REQUEST
        )

    def get_client_ip(self, request):
        """Extract client IP address from request."""
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip
```

### URL Configuration

```python
# cases/urls.py
from django.urls import path
from .api_views import FeedbackView

urlpatterns = [
    # ... existing patterns
    path('feedback/', FeedbackView.as_view(), name='feedback'),
]
```

### Rate Limiting Configuration

```python
# settings.py
REST_FRAMEWORK = {
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/hour',
        'feedback': '5/hour',  # Specific rate for feedback
    }
}
```

## Security Considerations

1. **Rate Limiting**: Prevent spam by limiting to 5 submissions per IP per hour
2. **Input Validation**: Validate all fields, sanitize HTML/scripts
3. **PII Protection**: Contact info is optional and stored securely
4. **IP Logging**: Store IP for rate limiting but respect privacy
5. **No Authentication**: Public endpoint to encourage feedback
6. **CORS**: Already configured for cross-origin requests

## Privacy Considerations

1. **Optional Contact Info**: Users can submit anonymously
2. **Data Retention**: Define retention policy (e.g., 2 years)
3. **PII Handling**: Contact methods may contain personal data
4. **GDPR Compliance**: Provide mechanism for data deletion requests
5. **Transparency**: Inform users how their data will be used

## Admin Interface

### Django Admin Configuration

```python
from django.contrib import admin
from .models import Feedback


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'feedback_type', 'subject', 'status',
        'has_contact_info', 'submitted_at'
    ]
    list_filter = ['feedback_type', 'status', 'submitted_at']
    search_fields = ['subject', 'description', 'related_page']
    readonly_fields = ['submitted_at', 'updated_at', 'ip_address', 'user_agent']

    fieldsets = (
        ('Feedback Details', {
            'fields': ('feedback_type', 'subject', 'description', 'related_page')
        }),
        ('Contact Information', {
            'fields': ('contact_info',),
            'classes': ('collapse',)
        }),
        ('Status', {
            'fields': ('status', 'admin_notes')
        }),
        ('Metadata', {
            'fields': ('ip_address', 'user_agent', 'submitted_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def has_contact_info(self, obj):
        return bool(obj.contact_info and obj.contact_info.get('contactMethods'))
    has_contact_info.boolean = True
    has_contact_info.short_description = 'Has Contact'
```

## Frontend Integration

### TypeScript Interface

```typescript
interface ContactMethod {
  type: 'email' | 'phone' | 'whatsapp' | 'instagram' | 'facebook' | 'other';
  value: string;
}

interface ContactInfo {
  name?: string;
  contactMethods?: ContactMethod[];
}

interface FeedbackSubmission {
  feedbackType: 'bug' | 'feature' | 'usability' | 'content' | 'general';
  subject: string;
  description: string;
  relatedPage?: string;
  contactInfo?: ContactInfo;
}

interface FeedbackResponse {
  id: number;
  feedbackType: string;
  subject: string;
  status: string;
  submittedAt: string;
  message: string;
}
```

### API Client

```typescript
const submitFeedback = async (feedback: FeedbackSubmission): Promise<FeedbackResponse> => {
  const response = await fetch('https://portal.jawafdehi.org/api/feedback/', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Accept': 'application/json',
    },
    body: JSON.stringify(feedback),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to submit feedback');
  }

  return response.json();
};
```

### React Hook

```typescript
import { useMutation } from '@tanstack/react-query';

const useFeedbackSubmission = () => {
  return useMutation({
    mutationFn: submitFeedback,
    onSuccess: (data) => {
      toast({
        title: t("feedback.submitted.title"),
        description: data.message,
      });
    },
    onError: (error: Error) => {
      toast({
        title: "Error",
        description: error.message,
        variant: "destructive",
      });
    },
  });
};
```

## Testing

### Test Cases

1. **Valid submission with all fields**: Verify 201 response
2. **Minimal submission (required fields only)**: Verify 201 response
3. **Anonymous submission (no contact info)**: Verify 201 response
4. **Missing required fields**: Verify 400 response with validation errors
5. **Invalid feedback type**: Verify 400 response
6. **Invalid contact method type**: Verify 400 response
7. **Rate limiting**: Verify 429 response after 5 submissions
8. **Subject too long**: Verify 400 response
9. **Description too long**: Verify 400 response
10. **Multiple contact methods**: Verify proper storage

### Sample Test

```python
import pytest
from rest_framework.test import APIClient
from cases.models import Feedback, FeedbackType, FeedbackStatus


@pytest.fixture
def api_client():
    return APIClient()


@pytest.mark.django_db
def test_submit_feedback_success(api_client):
    data = {
        "feedbackType": "bug",
        "subject": "Search not working",
        "description": "Detailed description of the bug",
        "relatedPage": "Cases page",
        "contactInfo": {
            "name": "राम बहादुर",
            "contactMethods": [
                {"type": "email", "value": "ram@example.com"}
            ]
        }
    }

    response = api_client.post('/api/feedback/', data, format='json')

    assert response.status_code == 201
    assert response.data['feedbackType'] == 'bug'
    assert response.data['subject'] == 'Search not working'
    assert response.data['status'] == 'submitted'
    assert 'submittedAt' in response.data
    assert 'message' in response.data

    # Verify database record
    feedback = Feedback.objects.get(id=response.data['id'])
    assert feedback.feedback_type == FeedbackType.BUG
    assert feedback.subject == 'Search not working'
    assert feedback.status == FeedbackStatus.SUBMITTED
    assert feedback.contact_info['name'] == 'राम बहादुर'


@pytest.mark.django_db
def test_submit_feedback_anonymous(api_client):
    data = {
        "feedbackType": "general",
        "subject": "Great platform",
        "description": "This is very helpful"
    }

    response = api_client.post('/api/feedback/', data, format='json')

    assert response.status_code == 201
    feedback = Feedback.objects.get(id=response.data['id'])
    assert feedback.contact_info == {}


@pytest.mark.django_db
def test_submit_feedback_missing_required_fields(api_client):
    data = {"feedbackType": "bug"}

    response = api_client.post('/api/feedback/', data, format='json')

    assert response.status_code == 400
    assert 'error' in response.data
    assert 'details' in response.data
    assert 'subject' in response.data['details']
    assert 'description' in response.data['details']


@pytest.mark.django_db
def test_submit_feedback_invalid_type(api_client):
    data = {
        "feedbackType": "invalid",
        "subject": "Test",
        "description": "Test description"
    }

    response = api_client.post('/api/feedback/', data, format='json')

    assert response.status_code == 400
    assert 'feedbackType' in response.data['details']


@pytest.mark.django_db
def test_feedback_rate_limiting(api_client):
    data = {
        "feedbackType": "general",
        "subject": "Test",
        "description": "Test description"
    }

    # Submit 5 times (should succeed)
    for _ in range(5):
        response = api_client.post('/api/feedback/', data, format='json')
        assert response.status_code == 201

    # 6th submission should be rate limited
    response = api_client.post('/api/feedback/', data, format='json')
    assert response.status_code == 429
```

## Monitoring and Analytics

### Metrics to Track

1. **Submission Volume**: Total feedback submissions per day/week/month
2. **Feedback Type Distribution**: Count by feedback type
3. **Response Rate**: Percentage of feedback with contact info
4. **Status Progression**: Time from submitted to resolved
5. **Rate Limit Hits**: Number of rate-limited requests
6. **Error Rate**: 4xx and 5xx response rates

### Dashboard Queries

```python
# Feedback by type
Feedback.objects.values('feedback_type').annotate(count=Count('id'))

# Feedback by status
Feedback.objects.values('status').annotate(count=Count('id'))

# Submissions over time
Feedback.objects.extra(
    select={'day': 'date(submitted_at)'}
).values('day').annotate(count=Count('id'))

# Average response time (submitted to resolved)
from django.db.models import Avg, F
Feedback.objects.filter(
    status=FeedbackStatus.RESOLVED
).annotate(
    response_time=F('updated_at') - F('submitted_at')
).aggregate(avg_time=Avg('response_time'))
```

## Future Enhancements

1. **Email Notifications**: Send confirmation emails when contact info provided
2. **Admin Dashboard**: Dedicated feedback management interface
3. **Feedback Voting**: Allow users to upvote existing feedback
4. **Public Roadmap**: Display feature requests and their status
5. **Automated Categorization**: Use ML to categorize feedback
6. **Integration with Issue Tracker**: Sync with GitHub/Jira
7. **Sentiment Analysis**: Analyze feedback sentiment
8. **Response Templates**: Quick responses for common feedback

## Changelog

### 1.0.0 (Initial Release)
- POST endpoint for feedback submission
- Support for 5 feedback types: bug, feature, usability, content, general
- Optional contact information with multiple contact methods
- Rate limiting: 5 submissions per IP per hour
- Status tracking: submitted, in_review, resolved, closed
- Admin interface for feedback management
- IP address and user agent logging for security
