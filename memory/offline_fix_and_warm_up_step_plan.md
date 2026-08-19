# Offline send-gate fix + dedicated lesson warm-up step

Two changes, planned together at your request. Part 1 is a live defect that makes the
desktop app unusable offline. Part 2 is the lesson-opener feature.

Paths note: app code is `ai_tutor/apps/...`, not `apps/...` — CLAUDE.md's shorthand
elides the package dir.

---

# Part 1 — The desktop app disables itself when the machine goes offline

## What's happening

`ai_tutor/static/js/network-helpers.js:29`

```js
function isOnline() {
  return navigator.onLine;
}
```

`ai_tutor/templates/tutoring/chat_tutor.html:2766`

```js
const isOffline = window.NetHelpers && !window.NetHelpers.isOnline();
if (isOffline) {
    const queueId = pushToQueue(text);   // localStorage — never POSTs
    markBubbleQueued(bubble, queueId);
    return;                              // the turn stops here
}
```

The desktop build serves from localhost and runs the tutor in local Ollama, so internet
connectivity has nothing to do with whether a turn can complete. But every send is
gated on `navigator.onLine`, so clicking an answer with Wi-Fi off files the message
into a localStorage outbox and no request ever reaches the server. That is the "no
response after clicking B" symptom, and the banner at `base.html:299` is the same
signal. The queue was written for students on flaky school Wi-Fi hitting the *hosted*
app; on desktop it switches the product off precisely when it should be at its best.

## Fix

`settings.DESKTOP_BUILD` already exists (`ai_tutor/config/settings.py:477`) and is
already exposed to every template as `is_desktop_build` by the
`accounts.context_processors.desktop_build` processor (registered at `settings.py:220`).
So no new plumbing is needed — use the server's own truth rather than sniffing
`location.hostname`.

1. **`base.html`** — before `network-helpers.js` loads, emit
   `window.AITUTOR_LOCAL_SERVER = {{ is_desktop_build|yesno:"true,false" }};`
   The inline `<script>` carries `nonce="{{ request.csp_nonce }}"` — a test enforces
   that on every inline script (CLAUDE.md, CSP section).
2. **`network-helpers.js:29`** — `isOnline()` returns `true` when
   `window.AITUTOR_LOCAL_SERVER` is set, otherwise `navigator.onLine`. This one change
   also fixes the banner, since `installOfflineBanner`'s `update(isOnline())`
   (`network-helpers.js:144`) reads the same helper.
3. **Purge, don't drain, on desktop.** A queue written before this fix will otherwise
   replay on the next load — your "B" is sitting in localStorage right now and would be
   sent against a question that is no longer live. On a desktop build, clear
   `aitutor.queue.<sessionId>` at chat init instead of draining it.

Consumers are contained: the only `isOnline()` callers are the chat queue
(`chat_tutor.html:2766`, `:2796`, `:2815`, `:2836`) and the banner. `fetchWithRetry`
and the exit-ticket modal don't use it.

## Also in scope — the connectivity probe

Neither of these blocks a turn, but both are wrong and both are in scope for this
change.

- **The connectivity probe reports offline even when online.**
  `simple_tutor/model_choice.py:47 _cloud_reachable()` HEADs `https://api.anthropic.com/`,
  which returns 404; `urlopen` raises on 4xx and the bare `except` calls that offline.
  Verified live on this machine. Consequence: `tutor_mode='auto'` — what both profiles
  in the device DB are set to — has never once selected the cloud tutor. Fix: treat an
  `HTTPError` as proof of connectivity.
- **DNS isn't bounded by the probe's timeout.** `urlopen(timeout=2)` covers socket
  connect/read, not `getaddrinfo`, which can stall for seconds per turn when Wi-Fi is
  associated with no upstream. Fix: resolve with an explicit short timeout, or skip the
  probe entirely on a desktop build where the local model is always available.

Everything else on the turn path is already offline-safe, verified by inspection:
regex intent classification (`intent.py`), ONNX embeddings with `HF_HUB_OFFLINE=1` /
`EMBEDDING_BACKEND=onnx` (`desktop_server.py:44`), a local judge row in the device DB,
no per-turn cloud judges wired into `simple_tutor`, sync on a background worker thread
(`desktop_server.py:268`), and no CDN or font references in any template.

Separately, remote lesson-image URLs won't load offline. That doesn't affect the text
reply.

## Models ship after installation, never in the bundle

**Direction: no model weights in the app bundle.** They are acquired after install.
This applies to the Piper voice *and* to the MiniLM ONNX encoder that is bundled
today.

The machinery already exists and this is an extension of it, not a new mechanism.
`apps/desktop/provisioning.py` acquires the 2.5 GB tutor model after install by two
routes, and its module docstring states the principle exactly:

> The installer deliberately does not carry the model: 2.5 GB makes an installer
> impractical to distribute and to re-download for every app update, while the weights
> themselves change rarely.

  - **From a file** — a GGUF from a USB stick, built by `manage.py build_model_bundle`.
    Works with no internet at any point, which is the pilot-school requirement.
  - **From the internet** — `ollama pull`. Convenience, never a dependency.

There is already a setup UI with `ProvisionState` progress polling
(`provisioning.py:72`, `views.py:35-62`, `start_install(source, path)` at `:304`).

### What changes

**1. Stop bundling.** Remove the MiniLM `datas` block at `AI-Tutor.spec:99-104` and do
not add one for Piper. Bundle drops 549 MB → **462 MB**.

**2. Generalise provisioning from "the model" to an asset manifest.** Entries carry
name, destination, size and checksum. The Ollama path keeps its `ollama create` step;
plain assets need only fetch-or-copy plus checksum verification, so they are a simpler
sibling of `install_from_file` / `install_from_registry`, not a rewrite of them.

| Asset | Size | Consumer |
|---|---|---|
| `minilm-l6-v2` ONNX encoder | 87 MB | KB retrieval + grader embedding gate |
| `en_US-lessac-medium` Piper voice | 60 MiB | TTS |
| `qwen3-4b-jetson` GGUF | ~2.5 GB | tutor (already provisioned this way) |

**3. Install to a writable location, not into the bundle.** A macOS `.app` is
read-only and code-signed — writing into it breaks the signature. Assets land in
`~/Library/Application Support/AI Tutor/models/`, beside the existing `tutor.db` and
`media/`.

**4. Resolve by settings, with fallbacks.** `kb_storage._onnx_dir()` (`:100-107`)
already prefers `settings.MINILM_ONNX_DIR` and falls back to `BASE_DIR/models` — point
it at the app-support path on desktop. Add `PIPER_VOICE_DIR` on the same shape and have
`audio_service.py:171 _PIPER_MODEL_DIRS` read it first, keeping `/models/piper` and
`~/.local/share/piper_voices` as fallbacks so the Jetson and container builds do not
regress.

**5. Extend the USB pack.** `build_model_bundle` must be able to emit the asset pack
too, or the zero-internet pilot schools can install the app and the tutor model but
never get embeddings or speech.

**6. Delete the silent download.** `audio_service.py:191 _download_piper_model` fetches
from huggingface.co on first use inside a request. Once provisioning owns acquisition,
that path should fail cleanly rather than block a request on a 60 MiB download.

### Lessons are gated on asset readiness

**A lesson cannot start unless every required asset is installed.** This replaces the
alternative — teaching on regardless and warning in the UI — which was the wrong shape:
without the encoder, `_retrieve_kb` catches its own failure and returns `[]`
(`engine.py:1829`), so an ungrounded lesson looks exactly like a grounded one to the
student, the teacher, and the transcript. A gate makes the failure impossible instead
of merely visible.

**Required vs optional.**

| Asset | Required to start a lesson? | Without it |
|---|---|---|
| `qwen3-4b-jetson` tutor model | **Yes** | no tutor at all |
| `minilm-l6-v2` encoder | **Yes** | silently ungrounded tutoring |
| `en_US-lessac-medium` voice | No | speaker button hidden; the lesson is unaffected |

Piper stays optional because this is a text tutor and blocking a school's lessons on a
missing TTS voice would cost far more than it protects. Flip it into the required set
if you'd rather.

**Readiness check** — `apps/desktop/readiness.py`, one function:

```python
def lesson_prerequisites() -> tuple[bool, list[str]]:   # (ready, missing_asset_names)
```

Reuses `provisioning.model_installed()` (`:134`) for the Ollama tag and plain file
existence for the encoder. It **fails closed**: any error means not ready, which is the
behaviour `model_installed` already has (`:140-141`). Cached for a few seconds — it runs
on lesson entry, not per turn.

**Enforced server-side at all three entry points**, so a deep link, a bookmark or a
stale tab cannot walk past it:

| Entry point | Behaviour when not ready |
|---|---|
| `views.py:599 chat_tutor_interface` (`/lesson/<id>/`, `/chat/lesson/<id>/`) | Redirect to the setup screen |
| `views.py:684 chat_start_session` + `chat_restart_session` | 409 with `{'missing': [...]}` |
| `views.py chat_respond` | 409 — blocks resuming a session if an asset is removed mid-lesson |

**Catalog** (`views.py:534`, `catalog.html`) — lesson cards render in a "Setup required"
state linking to the setup screen, so the gate is never a dead click.

**Desktop only.** The whole gate is behind `settings.DESKTOP_BUILD`. Hosted
deployments (Seychelles, Mozambique) have their assets by construction and must not
acquire a new failure mode.

---

# Part 2 — Warm-up as a real first step of every lesson

## Context

A lesson currently opens with an LLM greeting that goes straight into today's first
question. Nothing links the session to what the student already learned.

**Design (your direction): the warm-up is a dedicated `LessonStep` at the front of
every lesson, and it advances exactly like any other step.** The step owns the slot in
the flow; a deterministic server-side selector fills it with a question from the
student's prior lessons.

This deletes the hazard the earlier draft had to work around. `tools.py:1721
maybe_advance_step` advances on one correct verdict, counted by `tools.py:517
_current_step_correct_verdict_count` over turns tagged to the current step. With the
warm-up as its own step, a correct answer advances *out of the warm-up and into step 1*
— which is exactly what should happen. Advancement, the hint ladder, `InFlightQuestion`,
grading, the offline letter-picker and `SessionTurn.step` all work unmodified.

Decisions locked:

| Question | Decision |
|---|---|
| Prior-lesson source | Prerequisites first, then recency (last 5 completed) |
| Grading | Normal graded slot, normal hint ladder, no answer reveal |
| Question type | Whatever `_allowed_tutoring_types()` allows — not hardcoded to MCQ |
| Position | `order_index = 0`, every existing step shifted +1 |
| Prompt wording | **Yours.** This plan wires the branch and names the touchpoints |

## Renumbering — the load-bearing part

Steps are 0-based and dense (device DB: 1,980 steps, 221 lessons with a step 0). There
is **no unique constraint** on `(lesson, order_index)` — `LessonStep.Meta` declares only
`ordering = ['order_index']` (`curriculum/models.py:636`) — so a bulk
`update(order_index=F('order_index') + 1)` is safe and needs no ordering games.

The migration must also move **in-flight sessions**, or every active student resumes on
the wrong step: after the shift, old index N is now N+1. Bump
`TutorSession.current_step_index` by 1 for every session on a lesson that received a
warm-up step. Bumping sessions that sit past the last step (remediation / exit ticket)
keeps them past the end, which is correct. `SessionTurn.step` is an FK, so it follows
the row and needs nothing.

Three migrations:

- **A** (`curriculum`) — add `warm_up` to `LessonStep.StepType`.
- **B** (`curriculum`, data) — per lesson without one: shift `+1`, then create the
  warm-up at `order_index=0`. Reversible (delete warm-ups, shift `-1`).
- **C** (`curriculum`, data, depends on B and on tutoring's latest) —
  `apps.get_model('tutoring', 'TutorSession')`, bump `current_step_index += 1` for
  sessions on affected lessons.

B and C must be one atomic deploy step. There is a window during which old code sees
shifted steps; on both Container Apps and the desktop startup path migrations run
before the new revision serves, so the window is the deploy itself. Run B against a
copy of the prod dump first — CLAUDE.md requires it for backfills.

## The warm-up step row

`step_type='warm_up'`, `order_index=0`, `phase='engage'`, `enabling_objective=''`,
`question=''`. The row is a **container, not content**: a `LessonStep` is shared
curriculum, but a warm-up depends on what *this* student has already done, so the
question is resolved per session at runtime.

`content_generator.py` emits one for every newly generated lesson, so migration B is a
one-time catch-up rather than a recurring backfill.

## Selector — `ai_tutor/apps/tutoring/simple_tutor/warm_up.py` (new)

```python
def select_warm_up_question(session) -> ExitTicketQuestion | None
```

**Tier 1 — prerequisites.** `LessonPrerequisite.filter(lesson=session.lesson)`
(`skills_models.py:211`; 126 live rows in dev, populated by `detect_prerequisites`)
ordered `('-strength', '-is_direct')`, kept where the student has
`StudentLessonProgress.mastery_level='mastered'`.
`Course.prerequisites_enabled` is ignored — it gates access, not retrieval practice.

**Tier 2 — recency.** `StudentLessonProgress.filter(student=..., mastery_level='mastered')`
`.exclude(lesson=session.lesson).exclude(last_attempt_at__isnull=True)`
`.order_by('-last_attempt_at')[:5]`. Use `last_attempt_at`; `last_session_at` is a dead
field nothing writes.

**Institution scoping** through the curriculum side so platform-wide content stays
visible, copying `tutoring/views.py:143`:
`Q(lesson__unit__course__institution=inst) | Q(lesson__unit__course__institution__isnull=True)`.

**Question pick.** `ExitTicketQuestion.filter(exit_ticket__lesson=prior,
exit_ticket__assessment_type='exit_ticket', question_type__in=_allowed_tutoring_types())`
— filter on `assessment_type`, never `is_published` (`question_bank.py:180` explains
why). Drop figure-referencing stems (port the regex at `conversational_tutor.py:6949`),
since a prior lesson's figures aren't in this session's catalog. Prefer `easy`, then
`medium`, never `hard`.

**Determinism.** `random.Random(session.pk)` for both the lesson and the question —
the house convention (`tools.py:315`, `question_bank.py:155`). Same session → same
warm-up; a retake is a new session, so a new pick.

## Wiring

- **`tools.py:239 build_question_pool`** — branch at the top: when the resolved step is
  `step_type == 'warm_up'`, return `[select_warm_up_question(session)]` instead of the
  three current-lesson tiers. Everything downstream is untouched because the entry is a
  real `ExitTicketQuestion`.
- **Session start** (`engine.py:3215 start_for_view`) — start at `current_step_index=0`
  when the lesson has a warm-up step *and* the selector returns a question; otherwise
  start at `1`. A student on their first-ever lesson never lands on an empty warm-up
  step, so nothing can strand.
- **Step counter** — the warm-up is a step, so it counts. `1/5` becomes `1/6`. Simplest
  and honest.

## Prompt touchpoints — audit only, you write the text

Loaded `prompting-fundamentals-expert` and `claude-prompting-expert` first, per
CLAUDE.md.

**1. `engine.py:297 _OPENING_INSTRUCTION`** — the main one. It is a synthetic *user*
message, never persisted and never cached, so editing costs nothing in cache terms. It
already says "Greet the student briefly" and the model is skipping it. It needs two
variants, branched on whether the session started on the warm-up step: welcome the
student; frame the question as a recap from a *named previous lesson* and a bridge into
today; keep the existing contract (pose via `pose_question`, stem + options in the
visible reply, no asking permission to start). If you want "greet by name", the name has
to be in the prompt — `session.student.first_name` isn't there today, and this uncached
block is the safe place to interpolate it.

**2. `prompts.py:799 _render_current_step_block`** — `<current_step>` renders `<phase>`
and `<enabling_objective>`. On the warm-up step these should carry the *prior* lesson's
title and objective rather than blanks, so the tutor can name what it's recapping. I'll
pass the values through; any surrounding label is yours.

**3. `prompts.py:308` hint ladder** — no change needed as far as I can tell. It already
forbids revealing the answer, escalates on `attempt_count`, and pivots only when hints
stall — which is the "scaffold, never hand over the answer" behaviour you asked for.
Worth your read to confirm.

---

# Files

| File | Change |
|---|---|
| `ai_tutor/static/js/network-helpers.js` | `isOnline()` honours `window.AITUTOR_LOCAL_SERVER` |
| `ai_tutor/templates/base.html` | Emit the flag (nonce'd inline script); banner follows automatically |
| `ai_tutor/templates/tutoring/chat_tutor.html` | Purge the localStorage queue on a desktop build |
| `ai_tutor/apps/tutoring/simple_tutor/model_choice.py` | Probe: treat `HTTPError` as online; bound DNS or skip on desktop |
| `ai_tutor/apps/tutoring/audio_service.py` | `_PIPER_MODEL_DIRS` reads `PIPER_VOICE_DIR` first; drop the in-request HF download |
| `AI-Tutor.spec` | **Remove** the MiniLM `datas` block at `:99-104`; bundle no weights |
| `ai_tutor/config/settings.py` | `PIPER_VOICE_DIR` + point `MINILM_ONNX_DIR` at app-support on desktop |
| `ai_tutor/apps/desktop/provisioning.py` | Asset manifest alongside the Ollama path: fetch-or-copy + checksum |
| `ai_tutor/apps/desktop/views.py` + setup template | Per-asset status in the setup UI |
| `.../management/commands/build_model_bundle.py` | Emit the asset pack for the USB route |
| `ai_tutor/apps/desktop/readiness.py` | **New** — `lesson_prerequisites()`, fail-closed, briefly cached |
| `ai_tutor/apps/tutoring/views.py` | Gate `chat_tutor_interface`, `chat_start_session`, `chat_restart_session`, `chat_respond` |
| `ai_tutor/templates/tutoring/catalog.html` | "Setup required" card state linking to the setup screen |
| `ai_tutor/apps/curriculum/models.py` | `StepType` += `warm_up` |
| `ai_tutor/apps/curriculum/migrations/` | A (choices), B (shift + create), C (session index bump) |
| `ai_tutor/apps/tutoring/simple_tutor/warm_up.py` | **New** — selector, tiers, seeded pick, figure-stem filter |
| `ai_tutor/apps/tutoring/simple_tutor/tools.py` | `:239` warm-up branch in `build_question_pool` |
| `ai_tutor/apps/tutoring/simple_tutor/engine.py` | `:3215` start index; `:297` opening instruction (**your text**, my branch) |
| `ai_tutor/apps/tutoring/simple_tutor/prompts.py` | `:799` prior-lesson title/objective on the warm-up step |
| `ai_tutor/apps/curriculum/content_generator.py` | Emit a warm-up step for new lessons |
| `ai_tutor/apps/tutoring/tests/test_warm_up.py` | **New** |
| `ai_tutor/apps/safety/tests/` or chat tests | Desktop-build send-gate regression test |

No changes to `TutorSession.current_step_index`'s field type — it stays a
`PositiveIntegerField`, since the warm-up sits at 0.

---

# Verification

## Part 1 — offline

1. Run the desktop app, turn Wi-Fi **off**, open a lesson, click an answer.
   Expect: a real tutor reply, no banner, no queued badge. This is the exact
   reproduction from your screenshot.
2. Confirm via the server console that the POST to
   `/tutor/api/chat/<id>/respond/` actually arrives.
3. Turn Wi-Fi back on mid-lesson: no stale queued message replays.
4. Hosted web app (Wi-Fi off): the banner and queue still work — the fix is gated on
   `is_desktop_build`, so this must not regress.
5. Regression test asserting the send path is not gated on `navigator.onLine` when the
   desktop flag is set.
6. Probe: assert `_cloud_reachable()` returns True against an endpoint that answers
   404 — the case that is wrong today.
7. Assets: build the app with no weights and confirm it comes out at ~462 MB against
   the 549 MB baseline.
8. Install both assets from a **file** with the machine offline (the pilot-school
   route), then confirm the speaker button plays and KB retrieval returns chunks.
9. **Gate**: with the encoder absent, the catalog shows "Setup required", the lesson
   page redirects to setup, and a direct POST to `/tutor/api/chat/start/<id>/` returns
   409 with the missing-asset list — no lesson starts by any route.
10. Remove an asset mid-lesson: the next `chat_respond` is refused rather than serving
    an ungrounded turn.
11. Hosted web app (`DESKTOP_BUILD=False`): lessons start exactly as they do today —
    the gate must not reach Seychelles or Mozambique.

## Part 2 — warm-up

**Unit.**
- Selector: prereq tier wins when a prerequisite is mastered; falls to recency; returns
  `None` on a first-ever lesson.
- Determinism: same `session.pk` → same question; different pks spread across candidates.
- Institution scoping: another institution's lesson is never selected; a platform-wide
  (`institution=None`) one is.
- Type filter honours `TUTORING_QUESTION_TYPES` (set `mcq,short_answer`, confirm a
  `short_answer` warm-up can be picked).
- Advancement: a correct warm-up answer moves `current_step_index` 0 → 1, once.
- A lesson with a warm-up step but no eligible question starts the session at 1.
- Migration B: run forward on a copy of the prod dump, assert every lesson has exactly
  one `order_index=0` warm-up step and no gaps or collisions above it; run the reverse
  and assert the numbering is byte-identical to the starting state.
- Migration C: a session at index N before, N+1 after, on affected lessons only.

**Local end-to-end before commit** (CLAUDE.md bug-fix workflow):
1. `DATABASE_URL="sqlite:///$HOME/Library/Application Support/AI Tutor/tutor.db" venv/bin/python manage.py runserver 8877`
2. Sign in at `/student/login/` as `student`; open a lesson whose course has a mastered
   prerequisite. **Screenshot** the opener — greeting present, warm-up posed, prior
   lesson named.
3. Answer wrong: a hint arrives, no answer revealed, still on the warm-up step.
4. Answer right: the tutor moves into step 1 and teaches today's material.
5. Reload mid-warm-up: the resume path (`engine.py:3266`) returns the warm-up slot.
6. A student with no prior lessons: session starts at step 1, no error in the log.

Report the screenshots and the step-index transitions before pushing.
