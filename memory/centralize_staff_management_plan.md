# Centralize staff management into the Staff page — Plan (2026-06-11)

## Goal
One place for ALL staff/admin/teacher management: the existing **`/dashboard/staff/`**
(`dashboard:staff_list`, already in the sidebar). Move admin creation, teacher creation/invite,
pending-approval handling, pending-invite visibility, and password reset here. Remove the duplicate
"Manage Users" card from Settings.

## Current state (from audit)
Scattered across:
- **Staff page** `dashboard/staff_list.html` (view `views.py:4423`, `/dashboard/staff/`): staff table +
  Reset-password modal (calls `staff_reset_password_show/email`) + Delete (`delete_staff` `views.py:4463`) +
  "Invite Staff" button → `accounts:invite_staff`.
- **Settings "Manage Users" card** `settings.html:335-512`: `all_users` table (admins + staff), Status
  badges incl. Pending (`not u.is_active and not u.last_login`), actions `toggle_admin`/`toggle_user`/
  `delete_user`, the **create-admin form**, and the reset-offer panel I just added.
- **Settings POST actions** in `settings_page` (`views.py:3247-3296`): `create_admin`, `toggle_admin`,
  `toggle_user`, `delete_user` (all `is_superadmin`).
- **Invite flow** (apps/accounts): `invite_staff` (`views.py:490`, `/accounts/staff/invite/`) creates a
  `StaffInvitation` (token, email, role, is_used, registered_user, expires_at); `staff_register`
  (`/accounts/staff/register/<token>/`) consumes it (is_active=True); `staff_self_register`
  (`/accounts/staff/register/`) self-signup → is_active=False (pending approval).
- **Models**: `Membership`(role staff/student, is_active, password_reset_required, institution);
  `StaffInvitation`(email, role, token, is_used, registered_user, invited_by, expires_at).
- **Pending states**: (a) self-registered awaiting approval = `Membership.is_active=False` &
  `user.last_login is None`; (b) outstanding invite = `StaffInvitation.is_used=False`.
- **Nav**: `base.html:686-716` — "Staff" link (superadmin-only) sits under the "Settings" nav-section.

## Target design — Staff page becomes the hub (superadmin)
Rework `staff_list` view + `staff_list.html` into sections:
1. **Admins & active staff** — unified table (merge settings `all_users` + current staff table):
   Name · Email · Role (Super Admin / Staff) · School · Status · Actions
   (Reset password [show/email modal — exists], Promote/Demote [`toggle_admin`], Activate/Deactivate
   [`toggle_user`], Delete).
2. **Pending approval** — self-registered staff (`Membership.is_active=False`, `last_login None`):
   Approve (sets is_active=True) / Reject (delete).
3. **Pending invites** — `StaffInvitation.is_used=False`: email, role, invited_by, created, copy-link,
   Resend (re-email), Revoke (delete/mark used).
4. **Create Super Admin** form — MOVED from settings (with the existing reset-offer-on-existing-email).
5. **Invite teacher/staff** — invite form (email + school + role) creating a `StaffInvitation`, posting
   here (reuse `invite_staff` logic).

## Backend changes
- Move POST actions `create_admin` / `toggle_admin` / `toggle_user` / `delete_user` from
  `settings_page` → the `staff_list` view (single POST handler there). Keep the dedicated reset +
  `delete_staff` endpoints as-is.
- Expand `staff_list` context: `admins`, `active_staff`, `pending_approvals`, `pending_invites`.
- Add invite-create + invite-revoke handling to `staff_list` (or reuse `accounts:invite_staff` via a
  form that POSTs there and redirects back). Recommend: thin POST actions on staff_list that call the
  same StaffInvitation logic, to keep one page.
- **Repoint the reset-offer redirect**: my create-admin code currently does
  `redirect(settings?reset_user=<id>)` (`views.py:3247` block) → change to the staff page
  `?reset_user=<id>` once create_admin lives there. Carry `reset_target` into staff_list context.

## Settings changes
- Remove the entire "Manage Users" card (`settings.html:335-512`) + the 4 POST actions from
  `settings_page`. Settings keeps school/theme/prompts/model config only.

## Nav
- Keep the "Staff" sidebar link; optionally pull it out of the "Settings" nav-section into its own
  prominent item. Label "Staff".

## Out of scope (v1)
- Changing the registration/login or approval *semantics* (keep is_active-based approval).
- Per-teacher granular permissions. - Bulk staff import (separate bulk_student flow exists).
- Non-superadmin staff managing other staff (stays superadmin-only).

## Phased delivery
- **Phase 1**: unified Admins+Staff table on staff page + move create_admin + Pending-approval section;
  remove Manage Users from settings; repoint reset-offer. (~1 day)
- **Phase 2**: Pending-invites section + invite form on the staff page (consolidate the accounts invite
  UI); resend/revoke. (~0.5 day)

## Open questions
1. Fully REMOVE "Manage Users" from Settings (recommended, matches the ask) — confirm.
2. Invites: embed the invite form + pending-invites list directly on the Staff page (recommended) vs
   keep the separate `accounts:invite_staff` page and just link it.
3. Keep teacher self-registration (`staff_self_register`) enabled (pending-approval), or invite-only?

## Test before deploy
- Shell + test-client: each moved action works from the staff page; settings no longer exposes them.
- chrome-devtools visual: staff page sections render; create-admin + reset offer + approve + invite.
- Confirm reset-offer redirect now lands on the staff page.

## Next step
Confirm open questions, implement Phase 1, test locally + screenshot, deploy.
