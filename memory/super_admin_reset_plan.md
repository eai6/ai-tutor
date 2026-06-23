# Super Admin — create-admin "email exists" → offer reset (Plan, 2026-06-11)

## Problem
The "Create Super Admin" form (`templates/dashboard/settings.html:440`, action `create_admin`
in `apps/dashboard/views.py:3247`) **dead-ends** when the email already exists
(`messages.error("A user with email X already exists")`). Combined with a lost password and a
placeholder/undeliverable admin email, this strands the operator: can't create (exists), can't
log in (lost password). Scope (user-chosen): make the create-admin UI offer a **password reset**
for the existing account instead of erroring.

## Current state (audit)
- `create_admin` action: `views.py:3247-3265`. `username=email`, `is_staff=True`. Errors on existing email.
- Reusable reset infra ALREADY exists but is **wired to no template** (orphaned endpoints):
  - `staff_reset_password_show(user_id)` `views.py:4508` → POST, returns JSON
    `{username, temporary_password, must_change_on_next_login}`; sets temp pw + forces change.
  - `staff_reset_password_email(user_id)` `views.py:4553` → POST, emails a reset link (Django token).
  - URLs: `dashboard:staff_reset_password_show` / `_email` (`/dashboard/staff/<id>/reset-password/{show,email}/`).
  - `_flag_password_reset_required` forces change-on-next-login; `password_change_required` view + middleware.
- `settings_page` (`views.py:3108`) is Post/Redirect/Get → `return redirect('dashboard:settings')` (3446);
  GET builds `all_users` (3549) + context (3560) + renders `dashboard/settings.html` (3588).
- `reverse` imported (views.py:17). No existing fetch/JS in settings.html.

## Target design (3 edits, reuses existing endpoints)
1. **`create_admin` action** (views.py:3247): if `User.objects.filter(email__iexact=email).first()` exists,
   don't error — set a `messages.warning` and `redirect(f"{reverse('dashboard:settings')}?reset_user={id}")`.
   (Email required check first; password only required when actually creating.)
2. **`settings_page` GET**: read `request.GET.get('reset_user')`; if superadmin, fetch that User →
   `reset_target` in context (None otherwise).
3. **`settings.html`**: when `reset_target` set, render an offer panel above the create-admin form:
   - "Reset password (show temp)" → JS POST `staff_reset_password_show` → display the one-time temp pw.
   - "Email reset link" (if target.email) → JS POST `staff_reset_password_email` → "sent" confirmation.
   Small inline `<script>` with CSRF from `{{ csrf_token }}`; both endpoints already return JSON.

## Out of scope (per user's selection)
- `super_admin` management command (email-independent CLI recovery) — NOT chosen.
- Self-recovery/ACS email hardening — NOT chosen.
- Row-level reset buttons in the all_users table — trivial add-on, mention only.

## Immediate lockout (separate from this feature)
Operator regains access now via `az containerapp exec` (their TTY) → `python manage.py changepassword <email>`.
This UI fix requires being logged in, so it does not self-recover a fully-locked-out account.

## Test before deploy
- Django shell: existing-email path returns redirect w/ `?reset_user`; GET surfaces `reset_target`.
- Test-client render of `/dashboard/settings/?reset_user=<id>` as superadmin → 200, panel present, no template errors.
- chrome-devtools visual check of the offer panel + a reset action (temp pw shows).

## Next step
Implement the 3 edits, test locally, screenshot, then deploy to main (prod).
