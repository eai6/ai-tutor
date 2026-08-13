# accounts

Multi-tenancy, authentication, and user profile management.

Every record in the system is scoped to an `Institution` (school). Users belong to institutions via `Membership` records with role-based access control. Students self-register; staff are invited via secure tokens.

---

## Models

### Institution

The multi-tenancy root entity. Represents a school or organization.

| Field | Type | Description |
|-------|------|-------------|
| `name` | CharField(255) | School name |
| `slug` | SlugField (unique) | URL-friendly identifier |
| `timezone` | CharField(50) | Default: `UTC` |
| `is_active` | BooleanField | Soft-delete flag |

**Key methods:**
- `Institution.get_global()` -- Returns or creates the `global` institution used for platform-wide content (when `institution=None`).

### Membership

Links a Django `User` to an `Institution` with a specific role. A user can belong to multiple institutions with different roles.

| Field | Type | Description |
|-------|------|-------------|
| `user` | ForeignKey(User) | The Django user |
| `institution` | ForeignKey(Institution) | The school |
| `role` | CharField (`staff` / `student`) | Access level |
| `is_active` | BooleanField | Active membership flag |

**Constraints:** `unique_together = ['user', 'institution']` -- one role per institution per user.

### StudentProfile

Extended profile for students (OneToOne with `User`).

| Field | Type | Description |
|-------|------|-------------|
| `user` | OneToOneField(User) | Related name: `student_profile` |
| `school` | CharField(50) | School code (from PlatformConfig or hardcoded Seychelles defaults) |
| `grade_level` | CharField(5) | S1-S5 (Secondary 1 through 5) |

### PlatformConfig

Singleton (pk=1) for platform-wide configuration. Editable from the dashboard Settings page.

| Field | Type | Description |
|-------|------|-------------|
| `platform_name` | CharField(255) | Display name (default: "AI Tutor") |
| `logo` | ImageField | Platform logo |
| `primary_color` | CharField(7) | Hex color (default: `#E8590C`) |
| `secondary_color` | CharField(7) | Hex color (default: `#4ECDC4`) |
| `accent_color` | CharField(7) | Hex color (default: `#FFE66D`) |
| `schools` | JSONField | `[{"code": "...", "name": "..."}]` |
| `grades` | JSONField | `[{"code": "...", "name": "..."}]` |

**Resolution priority for school choices:**
1. Active `Institution` records from DB (excludes Global)
2. `PlatformConfig.schools` JSON
3. Hardcoded Seychelles defaults in `StudentProfile.DEFAULT_SCHOOL_CHOICES`

### StaffInvitation

Token-based invitation for staff members. Staff cannot self-register -- they must be invited.

| Field | Type | Description |
|-------|------|-------------|
| `institution` | ForeignKey(Institution) | Target school |
| `email` | EmailField | Optional recipient email |
| `role` | CharField | `staff` (default) |
| `token` | CharField(64, unique) | Secure invitation token |
| `invited_by` | ForeignKey(User) | Who created the invitation |
| `is_used` | BooleanField | Whether the token has been redeemed |
| `registered_user` | ForeignKey(User, nullable) | The user who registered via this token |
| `expires_at` | DateTimeField (nullable) | Optional expiration |

---

## Views

| View | Method | URL | Description |
|------|--------|-----|-------------|
| `landing_page` | GET | `/` | Redirects authenticated users by role; shows landing for anonymous |
| `student_login` | GET/POST | `/student/login/` | Username/password login with institution resolution |
| `student_register` | GET/POST | `/student/register/` | Self-registration with school and grade selection |
| `staff_login` | GET/POST | `/staff/login/` | Staff login with role verification |
| `staff_register` | GET/POST | `/staff/register/<token>/` | Invitation-only registration via `StaffInvitation` token |
| `invite_staff` | POST | `/staff/invite/` | Superadmin only: creates invitation tokens, optionally sends email |
| `logout_view` | GET | `/logout/` | Session cleanup and redirect |

### Validation Rules

- **Student registration**: username 3+ chars, password 6+ chars, school and grade required, email optional
- **Staff registration**: password 8+ chars, valid unused invitation token required
- **Login**: checks `Membership` exists and `is_active` for the resolved institution

---

## URL Patterns

```python
# apps/accounts/urls.py
/                               → landing_page
/student/login/                 → student_login
/student/register/              → student_register
/staff/login/                   → staff_login
/staff/register/<str:token>/    → staff_register
/staff/invite/                  → invite_staff
/logout/                        → logout_view
/register/                      → student_register  (legacy alias)
/login/                         → student_login      (legacy alias)
```

---

## Context Processor

### `institution_theme(request)`

Registered in `config/settings.py` TEMPLATES. Injects platform branding into every template context:

| Context Variable | Source | Description |
|-----------------|--------|-------------|
| `theme_primary` | `PlatformConfig.primary_color` | Primary brand color |
| `theme_secondary` | `PlatformConfig.secondary_color` | Secondary color |
| `theme_accent` | `PlatformConfig.accent_color` | Accent color |
| `theme_primary_dark` | Computed | Darkened primary (for hover states) |
| `theme_primary_light` | Computed | Lightened primary (for backgrounds) |
| `platform_logo_url` | `PlatformConfig.logo.url` | Logo URL (or None) |
| `platform_name` | `PlatformConfig.platform_name` | Platform display name |

Uses `_darken_hex()` and `_lighten_hex()` utility functions to compute color variants.

---

## Architecture Decisions

- **Multi-tenancy via foreign keys** rather than schema-per-tenant. Every queryable model has an `institution` FK.
- **Staff invitation-only** prevents unauthorized access to teacher dashboards.
- **PlatformConfig singleton** avoids the need for separate settings files for branding.
- **Global institution** (`slug='global'`) serves as a fallback for platform-wide content when `institution=None`.
