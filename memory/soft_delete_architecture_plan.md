# Soft-Delete Architecture — Plan (2026-04-28)

## Problem

Today, `Course.delete()`, `Lesson.delete()`, etc. CASCADE through the
ORM and irreversibly destroy student data — `StudentLessonProgress`,
`StudentSkillMastery`, `ExitTicketAttempt`, `TutorSession`,
`SessionMessage`. A single misclick on the teacher dashboard can wipe
a pilot's data with no recovery path.

The `StudentCompetencyRecord` work (see
`memory/student_competency_persistence_plan.md`) gives us a permanent
transcript that survives deletion, but that alone doesn't restore
sessions, attempt history, or the source course. We want a stronger
guarantee: **no destructive action on student or school data is ever
final until 30 days have passed.**

## Architectural rule (from the user, 2026-04-28)

> Soft-delete student or school data — never hard delete.
> Soft-delete courses for 30 days, then hard-delete afterwards.

## Design

### Model layer — `SoftDeleteMixin`

`apps/common/soft_delete.py` (new module):

```python
class SoftDeleteQuerySet(models.QuerySet):
    def alive(self):    return self.filter(deleted_at__isnull=True)
    def trashed(self):  return self.filter(deleted_at__isnull=False)
    def hard_delete(self): return super().delete()
    def delete(self):
        # Soft-delete by default at the queryset level.
        return self.update(deleted_at=timezone.now())

class ActiveManager(models.Manager):
    """Default manager — excludes soft-deleted rows."""
    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db).alive()

class AllObjectsManager(models.Manager):
    """Use for admin / restore / cron purge views."""
    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db)

class SoftDeleteMixin(models.Model):
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )

    objects = ActiveManager()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    def soft_delete(self, by=None):
        self.deleted_at = timezone.now()
        if by:
            self.deleted_by = by
        self.save(update_fields=['deleted_at', 'deleted_by'])

    def restore(self):
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=['deleted_at', 'deleted_by'])

    def delete(self, using=None, keep_parents=False):
        # Default `delete()` becomes soft-delete. Callers that
        # genuinely want a hard delete (cron purge, test teardown)
        # call `.hard_delete()` explicitly.
        return self.soft_delete()

    def hard_delete(self, *args, **kwargs):
        return super().delete(*args, **kwargs)
```

### Models that adopt the mixin

Tier 1 — student data (never hard-deletes via UI; cron is a no-op):
- `User` / `accounts.StudentProfile`
- `tutoring.TutorSession`
- `tutoring.SessionMessage` (only if its TutorSession is soft-deleted)
- `tutoring.StudentLessonProgress`
- `tutoring.skills_models.StudentSkillMastery`
- `tutoring.skills_models.SkillPracticeLog`
- `tutoring.ExitTicketAttempt`
- `tutoring.skills_models.StudentCompetencyRecord` (already append-only — soft-delete is a defensive layer)
- `tutoring.skills_models.StudentAchievement`

Tier 2 — school data (admin-only; cron never purges):
- `accounts.Institution`

Tier 3 — course data (30-day grace, then cron hard-deletes):
- `curriculum.Course`
- `curriculum.Unit`
- `curriculum.Lesson`
- `curriculum.LessonStep`
- `curriculum.Skill`
- `tutoring.ExitTicket` + `ExitTicketQuestion`
- `dashboard.WeeklyAssignment` (course-derived)

Out — content already easy to recover from elsewhere:
- `dashboard.CurriculumUpload` (the source files live in blob storage)
- `dashboard.TeachingMaterialUpload` (ditto)
- `media_library.MediaAsset` (regeneratable from images on disk)

### CASCADE rules — the load-bearing change

A soft-deleted course must NOT cascade through CASCADE FKs and
delete student data. Two options:

1. **Migrate FKs from CASCADE → SET_NULL on student-data models.**
   `StudentLessonProgress.lesson` becomes nullable + `SET_NULL`. Same
   for `StudentSkillMastery.skill`, `TutorSession.lesson`,
   `ExitTicketAttempt.exit_ticket`, etc. Now even a HARD delete
   (cron after 30 days) leaves student data with a null pointer +
   the snapshot fields on `StudentCompetencyRecord` for context.
2. **Override `delete()` on parent models** to refuse if children
   exist, force teacher to soft-delete instead.

Recommend (1) — it's simpler and aligns with how
`StudentCompetencyRecord` is already designed (`SET_NULL` + snapshot
fields on the source FKs).

### Unique-constraint adjustments

`unique_together` constraints become tricky with soft-delete: a
soft-deleted row + a new live row with the same key = legitimate.
Fix: replace unique_together with partial unique indexes that
exclude soft-deleted rows. PostgreSQL supports this natively via
`UniqueConstraint(condition=Q(deleted_at__isnull=True))`.

Audit needed: every `unique_together` and `UniqueConstraint` on a
soft-deleted model gets a partial-index migration.

### Query layer — the audit task

Every `.objects` call on a soft-deleted model now silently filters
to alive rows. Every code path that needs to see deleted rows
(admin "Trash" view, restore, purge cron, test teardown) must
explicitly use `.all_objects`. Estimated audit scope: ~150–200
query call-sites across the codebase. Can be done incrementally
behind feature flags per model.

Critical paths to audit first:
- `apps/tutoring/views.py` (catalog, course detail, session start)
- `apps/dashboard/views.py` (teacher dashboard)
- `apps/tutoring/conversational_tutor.py` (engine reads lesson + steps)
- Admin pages

### UI changes

Teacher dashboard:
- "Delete course" button calls soft-delete, shows
  "🗑 Course moved to trash. Will be permanently deleted in 30 days."
- New "Trash" tab shows soft-deleted courses with "Restore" /
  "Delete forever" buttons.
- After 30 days, course disappears (cron hard-deleted it).

Admin / superadmin:
- Equivalent "Trash" view per model.
- "Audit log" of soft-delete events (deleted_by, deleted_at).

### Cron purge

`apps/tutoring/management/commands/purge_soft_deleted_courses.py`:

```
for course in Course.all_objects.filter(
    deleted_at__lt=timezone.now() - timedelta(days=30)
):
    # Skip if any child still has student activity
    # Then call .hard_delete() — student data survives via SET_NULL
    course.hard_delete()
```

Schedule via Azure Container Apps Job or a cron entry on the
container. Daily run is sufficient.

Tier 1 (student) and Tier 2 (school) data is **never** purged by
cron. Hard-deleting student/school requires an explicit
`manage.py shell` action with confirmation — defensive.

## Phased delivery

| Phase | Work | Solo-dev days |
|-------|------|---------------|
| **A** | `SoftDeleteMixin` module, tests, no model adoption yet | 0.5 |
| **B** | Adopt on `Course` + cascade-FK migrations to `SET_NULL` on student-data models. Migrate `unique_together` → partial unique indexes. Smoke test on dev DB | 1.5 |
| **C** | Wire teacher dashboard: soft-delete UI, "Trash" tab, restore button | 1 |
| **D** | Cron purge command + Azure schedule | 0.5 |
| **E** | Adopt on Tier 1 (student data) — admin only, no UI change for users. Audit query call-sites | 1 |
| **F** | Adopt on Tier 2 (Institution) — superadmin only, never auto-purged | 0.5 |

Total: ~5 focused days. Can land in two PRs (A+B+C+D as one ship,
E+F as a follow-up).

## Open questions

1. **Hard-delete for legitimate cases (GDPR / pilot data scrub)?**
   Recommend: keep `hard_delete()` available via management command
   only, never via UI. Document the legal-request workflow separately.

2. **What about `User.delete()` (account closure)?**
   Recommend: soft-delete + anonymise PII (name, email) but keep the
   row + cascading data. A separate "right-to-be-forgotten"
   command does the deeper PII scrub.

3. **Storage cost — soft-deleted rows accumulate.**
   Pilot scale (~50 students, single school) is negligible. At Tanzania
   pilot scale, revisit. The 30-day cron on courses bounds the largest
   tables.

4. **Test isolation** — pytest fixtures often use
   `Model.objects.all().delete()` for teardown. With soft-delete this
   becomes a no-op for next-test cleanup.
   Recommend: pytest fixtures call `.hard_delete()` or
   `.all_objects.all().delete()` in teardown.

## Out of scope

- Soft-delete for `dashboard.CurriculumUpload`, `TeachingMaterialUpload`,
  `MediaAsset`, `FeedbackReport`. Recoverable from source / regeneratable.
- Audit-log model. Basic `deleted_by` + `deleted_at` is enough; richer
  audit can come later.
- Multi-region soft-delete sync. Not relevant — single Azure region.
- Restoring purged courses. Once cron has hard-deleted, gone.
  StudentCompetencyRecord snapshots provide the only history.

## Why the StudentCompetencyRecord work still matters

Soft-delete is a recoverability layer; the transcript is a
**compatibility** layer. After 30 days, courses do hard-delete
(and Tier 2 admin actions can hard-delete schools); the transcript is
the only source of truth for "what did this student earn from
[that course / that school] before it was removed?"

The two systems are complementary, not redundant.

## Next step

Phase A: implement `apps/common/soft_delete.py` with tests, no model
adoption. Land that as a small, reviewable PR. Then Phase B — adopt
on `Course` + the cascade-FK migrations — as a separate, more
involved PR.
