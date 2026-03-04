# safety

Content safety, compliance monitoring, GDPR data protection, and child safety.

This app provides a comprehensive safety layer: content filtering for student messages and AI responses, rate limiting, age verification (COPPA/GDPR), audit logging, consent management, and data export/deletion for privacy compliance.

---

## Models

### SafetyAuditLog

Event log for safety-related incidents.

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | DateTimeField | Auto-set on creation |
| `event_type` | CharField | See event types below |
| `user_id` | IntegerField (nullable) | Raw user ID |
| `user_hash` | CharField(16) | SHA-256 anonymized user identifier |
| `session_id` | IntegerField (nullable) | Related tutoring session |
| `details` | JSONField | Event-specific data |
| `ip_address` | GenericIPAddressField | Client IP |
| `user_agent` | TextField | Client user agent string |
| `severity` | CharField | `info`, `warning`, `critical` |

**Event types:**
| Event | Description |
|-------|-------------|
| `content_flagged` | Harmful/inappropriate content detected |
| `rate_limited` | Rate limit exceeded |
| `age_check` | Age verification performed |
| `data_export` | User data exported (GDPR) |
| `data_delete` | User data deleted (GDPR) |
| `login_attempt` | Authentication attempt logged |
| `consent_given` | Consent granted |
| `consent_withdrawn` | Consent revoked |
| `data_cleanup` | Automated data cleanup ran |

**Indexes:** `(event_type, timestamp)`, `(user_hash, timestamp)`, `(severity, timestamp)`

### ConsentRecord

GDPR consent tracking per user.

| Field | Type | Description |
|-------|------|-------------|
| `user` | ForeignKey(User) | The user |
| `consent_type` | CharField | `data_processing`, `ai_tutoring`, `analytics`, `parental` |
| `given` | BooleanField | Current consent state |
| `given_at` | DateTimeField | When consent was granted |
| `withdrawn_at` | DateTimeField | When consent was revoked |
| `parent_email` | EmailField | For parental consent |
| `parent_name` | CharField(100) | For parental consent |
| `ip_address` | GenericIPAddressField | IP at time of consent |

**Constraint:** `unique_together = ['user', 'consent_type']`

---

## Safety Module (`__init__.py`)

The `__init__.py` file (~600 lines) contains the core safety infrastructure.

### SafetyConfig

Global constants for safety thresholds:

| Category | Setting | Value |
|----------|---------|-------|
| Rate limits | Per minute | 10 messages |
| Rate limits | Per hour | 100 messages |
| Rate limits | Per day | 500 messages |
| Rate limits | Sessions per day | 20 |
| Message limits | Max length | 2,000 characters |
| Message limits | Max tokens | 500 input tokens |
| Data retention | Conversations | 90 days |
| Data retention | Audit logs | 365 days |
| Data retention | Anonymize after | 30 days |
| Child protection | Min age (COPPA) | 13 years |
| Child protection | Parental consent (GDPR) | Under 16 years |

### ContentSafetyFilter

Analyzes student messages and AI responses for unsafe content.

**Detection categories:**

| Category | Examples | Action |
|----------|----------|--------|
| PII | SSN, credit card, email, phone, address | Flag + redact |
| Harmful content | kill, hurt, weapon, suicide, self-harm, bomb | Flag + block |
| Manipulation | "ignore instructions", "pretend you're not", "jailbreak", "DAN" | Flag + block |
| Off-topic | Dating, gambling, drugs, alcohol | Flag + redirect |

**API:**
```python
result = ContentSafetyFilter.check_content(content, context)
# Returns: SafetyCheckResult(is_safe, flags, filtered_content, warnings)

response = ContentSafetyFilter.get_safe_response(flag)
# Returns age-appropriate response template for the specific flag type
```

### ChildProtection

Age-related compliance checks.

| Method | Description |
|--------|-------------|
| `check_age_compliance(user)` | Validates minimum age (13) and parental consent requirement (under 16) |
| `get_age_appropriate_system_prompt()` | Returns child safety guidelines for LLM system prompt injection |
| `filter_ai_response_for_children()` | Redacts potentially unsafe content from AI responses |

### RateLimiter

Cache-based rate limiting per user.

| Method | Description |
|--------|-------------|
| `check_rate_limit(user_id)` | Returns `(allowed: bool, reason: Optional[str])` |
| `record_message(user_id)` | Increments per-minute/hour/day counters in cache |
| `@rate_limit_required` | View decorator: returns 429 if limit exceeded |

### DataPrivacy

GDPR compliance utilities.

| Method | Description |
|--------|-------------|
| `anonymize_user_id(user_id)` | SHA-256 hash with `SECRET_KEY` salt |
| `anonymize_content(content)` | Redacts PII patterns from text |
| `cleanup_old_data()` | Deletes turns/sessions/logs older than retention period |
| `export_user_data(user)` | Full GDPR export: user info, sessions, turns, progress, consent |
| `delete_user_data(user, keep_anonymized)` | GDPR erasure: full delete or anonymize |

### ImageSafetyFilter

Validates image generation prompts before sending to Gemini.

**Blocked content:** nudity, sexual content, violence, gore, weapons, drugs, celebrities, scary/horror imagery.

```python
result = ImageSafetyFilter.check_image_request(prompt, lesson_title, subject)
# Returns: SafetyCheckResult
```

### SafetyMiddleware

Django middleware registered in `MIDDLEWARE` setting.

**Behavior:**
- Intercepts requests to `/tutor/api/` endpoints
- Checks age compliance for authenticated users
- Logs warnings for non-compliant users
- Returns 403 if age requirements not met

---

## Views

| View | Method | URL | Description |
|------|--------|-----|-------------|
| `privacy_dashboard` | GET | `/privacy/` | Shows user's data and consent status. Auto-creates missing `ConsentRecord` entries. |
| `update_consent` | POST | `/consent/<type>/` | Toggle a specific consent type. Logs to `SafetyAuditLog`. |
| `export_my_data` | GET | `/export/` | GDPR data portability: exports all user data as JSON download. |
| `delete_my_data` | POST | `/delete/` | GDPR right to erasure. Supports full delete or anonymization. |
| `privacy_policy` | GET | `/privacy-policy/` | Static privacy policy page. |
| `terms_of_service` | GET | `/terms/` | Static terms of service page. |
| `parental_consent_form` | GET/POST | `/parental-consent/` | Collects parent email/name for users under 16. |

All safety URLs are mounted under the root URL namespace (not `/safety/`), accessed via `include('apps.safety.urls')` in `config/urls.py`.

---

## Management Commands

| Command | Description |
|---------|-------------|
| `delete_user_data --user <id>` | GDPR-compliant data deletion for a specific user |
| `export_user_data --user <id>` | Export all data for a user as JSON |
| `safety_cleanup --days <n>` | Delete audit log entries older than N days |

---

## Architecture Decisions

- **Regex-based content filtering** rather than LLM-based classification -- lower latency, no API cost, deterministic. The patterns cover common PII formats and known harmful/manipulation phrases.
- **Cache-based rate limiting** -- Uses Django's cache framework (per-minute/hour/day counters) rather than a dedicated rate limiting service. Simple and effective for the current scale.
- **Anonymized audit logging** -- `user_hash` (SHA-256 of user ID + salt) allows correlating events without storing raw user IDs in the audit log.
- **Auto-creating ConsentRecord** -- The privacy dashboard creates missing consent records on first visit, ensuring every user has a complete consent profile.
- **Middleware for age checks** -- `SafetyMiddleware` runs on every tutoring API request to enforce COPPA/GDPR age compliance at the HTTP layer, before any LLM interaction.
- **Data retention automation** -- `cleanup_old_data()` enforces configurable retention periods (90 days for conversations, 365 days for audit logs, anonymize after 30 days).
