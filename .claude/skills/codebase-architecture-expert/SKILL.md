---
name: codebase-architecture-expert
description: Expert on the architectural patterns of THIS AI Tutor Django codebase — how its abstractions, hierarchies, and module boundaries are shaped. Auto-loads when designing new abstractions, evaluating refactors, extending core systems, or adding a new pluggable component. Covers BaseLLMClient + factory dispatch, judges/ decomposition + combined_judge orchestration, curriculum hierarchy (Course→Unit→Lesson→LessonStep), session state model (TutorSession + SessionTurn + engine_state JSON), multi-tenancy via institution scoping, ModelConfig/PromptPack purpose-based dispatch. Tells future Claude what TO mirror and what NOT to copy.
---

# AI Tutor Codebase Architecture — Expert

How this codebase actually decomposes. Mirror these patterns when adding new components; avoid the inconsistencies flagged at the bottom.

## The seven load-bearing patterns

### 1. Pluggable LLM abstraction (`apps/llm/client.py`)

**Where it lives.** `apps/llm/client.py:38` defines `BaseLLMClient` as an abstract base class. Concrete subclasses: `AnthropicClient`, `OpenAIClient`, `GeminiClient`, `OllamaClient`, `MockLLMClient` (for tests).

**The contract.**

```python
class BaseLLMClient(ABC):
    @abstractmethod
    def generate(
        self,
        messages: list[dict],   # role/content format
        system_prompt: str,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        ...

    # Optional override — falls back to generate() if not implemented
    def generate_stream(self, ...) -> Iterator[str]:
        ...
```

`LLMResponse` is a dataclass: `content, tokens_in, tokens_out, model, stop_reason`.

**Dispatch.** Factory function `get_llm_client(config: ModelConfig, use_mock=bool)` (line 714) switches on `config.provider`. **No service locator, no DI container.** Callers pass a `ModelConfig`; the factory returns the right client.

**To mirror this pattern** (adding a new pluggable component):
1. Define a dataclass for the response shape.
2. Define an ABC with `@abstractmethod` for the single core operation.
3. Subclass per provider, isolate retry/gating logic per subclass.
4. Add a factory function that dispatches on a config field.
5. Provide a `MockXClient` for tests.

**What NOT to copy.** `AnthropicClient.generate_with_tools()` is provider-specific and NOT on the base interface. Callers must check provider before calling. If you need a "tools" interface, design it cross-provider from the start.

### 2. Judge orchestration (`apps/tutoring/judges/`)

**Where it lives.** Sub-package `apps/tutoring/judges/` with one judge per file: `arithmetic.py`, `factual.py`, `rule.py`, `step_eval.py`, `coherence.py`, `figure_ref.py`, `figure_vision.py`, `safety.py`. Orchestrator in `apps/tutoring/judges/__init__.py::run_all_judges()`. Compat wrapper at `apps/tutoring/combined_judge.py::run_combined_judge()`.

**The contract per judge.**
- Each exposes one entry function returning a typed dataclass result.
- Each result has `skipped` + `skip_reason` fields so the orchestrator knows when to ignore.
- Judges never call each other. Independence is the whole point.

**Orchestration.** `run_all_judges()` uses `ThreadPoolExecutor(max_workers=8)` to run them concurrently. Each judge is **fail-soft**: an exception skips that judge; others continue. Results merge into `CombinedJudgeResult`.

**Result shape** (`apps/tutoring/combined_judge.py:55`). 26 fields, grouped by judge. Two serializers:
- `to_metadata()` — flattened for legacy teacher dashboard.
- `to_judge_outputs()` — structured per-judge breakdown for the benchmark eval tooling (`SessionTurn.judge_outputs`).

**To mirror this pattern** (adding a new judge):
1. Create `apps/tutoring/judges/<name>.py` with one function and one dataclass result.
2. Return early with `skipped=True, skip_reason="..."` when not applicable.
3. Register in `run_all_judges()`.
4. Add the result fields to `CombinedJudgeResult`.
5. Update **both** `to_metadata()` (if dashboard cares) and `to_judge_outputs()` (always).

**What NOT to copy.** `CombinedJudgeResult`'s 26 flat fields are bloated — fields like `figure_ref_in_question` only apply when figures are involved. If designing a fresh result type, prefer nested per-judge sub-results (`{step_eval: {...}, arithmetic: {...}}` like `to_judge_outputs()` already does).

### 3. Curriculum hierarchy (`apps/curriculum/models.py`)

**The hierarchy.**

```
Course (line 15)
  └─ Unit (line 102)
        └─ Lesson (line 142)
              ├─ LessonStep (line 253)          ← teaching content
              └─ ExitTicket → ExitTicketQuestion ← assessment bank
```

**Key fields per level.**

| Model | Identity | State |
|---|---|---|
| `Course` | `title`, `grade_level`, `subject_type` (MATH/SCIENCE/HUMANITIES/...), `institution FK` (nullable = platform-wide) | — |
| `Unit` | `order_index`, `grade_level`, `terminal_objectives`, `enabling_objectives` (JSON) | — |
| `Lesson` | `objective`, `enabling_objectives` (JSON), `estimated_minutes` | `content_status` (EMPTY/GENERATING/READY/READY_WITH_WARNINGS/FAILED), `content_quality` (TIER_1..4), `teacher_approved` |
| `LessonStep` | `order_index`, `step_type` (TEACH/WORKED_EXAMPLE/PRACTICE/QUIZ/SUMMARY), `answer_type`, `concept_tag`, `enabling_objective` | `priority` (REQUIRED=1, CORE=2, ENRICHMENT=3), `media`/`hints`/`rubric` JSON |

**Priority-based runtime selection.** `LessonStep.priority` lets short sessions drop ENRICHMENT first, then CORE, never REQUIRED. The engine doesn't re-plan — it just filters. **Mirror this pattern** when adding flexible-length flows: tag content with priority, let runtime drop.

**Content state machine.** `Lesson.content_status` is the most reliable state machine in the codebase. Background generation transitions `EMPTY → GENERATING → {READY, FAILED}`. Stuck `GENERATING` lessons must be manually reset (see `CLAUDE.md`).

**What NOT to copy.** JSONFields on `LessonStep` (`media`, `educational_content`, `curriculum_context`) are **untyped and unvalidated**. Future schemas should use Pydantic or `JSONField` with explicit serializers.

### 4. Session state architecture (`apps/tutoring/models.py`)

**The split.**

```
TutorSession                ← one per lesson attempt
  ├─ engine_state JSON      ← untyped runtime state (LATEST only, no per-turn history)
  ├─ status enum            ← ACTIVE / COMPLETED / ABANDONED
  ├─ current_step_index     ← typed; mirrors engine_state for legacy queries
  │
  └─ SessionTurn[]          ← one per message (student/tutor/system)
        ├─ metadata JSON    ← per-turn analysis (is_correct, eval_layer, validator_issues, ...)
        ├─ judge_outputs JSON ← structured per-judge breakdown (Combined­Judge­Result.to_judge_outputs())
        └─ step FK          ← which LessonStep this turn belongs to (often NULL)
```

**SessionState enum** (`conversational_tutor.py` ~line 100):

```python
class SessionState(Enum):
    TUTORING = "tutoring"
    EXIT_TICKET = "exit_ticket"
    COMPLETED = "completed"
```

Lives inside `engine_state['session_state']`. The display-level 5E phase ("Engage", "Explore", "Explain", "Practice", "Evaluate") comes from each `LessonStep.phase` — NOT from the session state. **Don't reintroduce phase-based flow control.**

**Where state lives.**
| State | Where |
|---|---|
| Current step | `TutorSession.current_step_index` + `engine_state['current_topic_index']` (both kept in sync) |
| Session phase | `engine_state['session_state']` |
| Per-turn verdict | `SessionTurn.metadata['is_correct']`, `eval_layer`, `eval_reasoning` |
| Per-judge breakdown | `SessionTurn.judge_outputs` (post-2026-05-11 turns) |
| Bank state | `engine_state['question_pool_ids']`, `engine_state['rendered_bank_ids']` |
| Exit ticket | `engine_state['exit_ticket_id']`, `exit_ticket_selected_ids` |

**Mirror this pattern** when adding session-wide state: typed columns for things the dashboard queries; `engine_state` JSON for things only the engine reads.

**What NOT to copy.** `engine_state` is untyped — fragile to key renames. Backward-compatibility hacks like mapping old `phase` keys to `SessionState` exist because of past schema churn. **Don't add new keys without writing them down somewhere documented.**

### 5. Multi-tenancy via institution scoping

**The convention.**

```python
# Every user-facing model has institution FK
class Course(models.Model):
    institution = models.ForeignKey(
        'accounts.Institution', null=True, blank=True,  # NULL = platform-wide
        ...
    )
```

**The query pattern** (appears 10+ places, e.g., `apps/tutoring/views.py:73,87,293,331`):

```python
Q(institution=user_inst) | Q(institution__isnull=True)
```

**Reads as:** "show this institution's content plus platform-wide content." Missing this filter = cross-school data leak.

**The global fallback.** `Institution.get_global()` returns the special "all schools" institution. Used when content is uploaded in "All Schools" mode but operations need a non-null FK.

**ChromaDB normalization.** `apps/curriculum/knowledge_base.py:106,114` — `None` is normalized to `GLOBAL_INSTITUTION_ID = 0` for vector storage. Don't bypass this; the persist_directory path uses it.

**To mirror this pattern.** Any new user-facing model:
1. Add `institution = models.ForeignKey(... null=True, blank=True)` if it can be platform-wide.
2. In every query, use `Q(institution=inst) | Q(institution__isnull=True)`.
3. If indexing to ChromaDB, normalize `None → GLOBAL_INSTITUTION_ID`.

**What NOT to copy.** The query pattern is duplicated 10+ times with no helper. If you find yourself adding it a 4th time, **extract a manager method** (e.g., `Course.objects.visible_to(institution)`).

### 6. Purpose-based config & dispatch (`apps/llm/models.py`)

**ModelConfig per purpose.**

```python
class Purpose(TextChoices):
    GENERATION = 'generation'        # Curriculum + lesson generation
    TUTORING = 'tutoring'            # Student chat
    EXIT_TICKETS = 'exit_tickets'
    SKILL_EXTRACTION = 'skill_extraction'
    IMAGE_GENERATION = 'image_generation'
    JUDGE = 'judge'                  # Post-response sanity check
    REGEN = 'regen'                  # Ensemble rewrite
```

Each `ModelConfig` row binds a purpose to a provider + model + temperature + API key. Active config per purpose retrieved via `ModelConfig.get_for(purpose)`.

**PromptPack per institution.** Layered prompts:
- `system_prompt`, `teaching_style_prompt`, `safety_prompt`, `format_rules_prompt` — composed at runtime.
- Extended overrides: `tutor_system_prompt`, `content_generation_prompt`, etc.
- Placeholder support: `{institution_name}`, `{tutor_name}`, `{language}`, `{grade_level}`.

**API key storage.** Primary: Fernet-encrypted DB field. Fallback: env var named in `api_key_env_var`. `ModelConfig.get_api_key()` tries decryption, then env.

**To mirror this pattern.** Anywhere you need runtime-configurable behavior per purpose:
1. Add a `Purpose` enum value.
2. Add a config row in admin/seed.
3. Read via `ModelConfig.get_for(purpose)` — never hardcode model IDs in code.

**What NOT to copy.** PromptPack placeholder substitution is string-replace with **no validation**. If `{institution_name}` is missing, the literal `{institution_name}` ships to the user. Either validate at save-time or use a templating engine.

### 7. App boundaries (`apps/`)

**Dependency direction** (verified — no circular references):

```
accounts          ← no external deps; everything FKs here
   ↓
curriculum, llm, safety, media_library
   ↓
tutoring          ← uses curriculum + llm
   ↓
dashboard, api    ← uses tutoring + curriculum
```

**Public surface per app.**
- `apps/accounts` exports `User`, `Institution`, `Membership`, `StudentProfile`.
- `apps/curriculum` exports models + `CurriculumKnowledgeBase`.
- `apps/llm` exports `BaseLLMClient`, `ModelConfig`, `PromptPack`, `MobileInferenceModel`.
- `apps/tutoring` exports models + `judges/` package + `validator.py` + `conversational_tutor.py`.

**To mirror this pattern.** New apps:
1. Decide what depends on it (look at the layer above its dependencies).
2. Export only the stable surface; keep internals truly internal.
3. **Never** introduce a circular import — if you need to, the boundary is wrong.

## Notable inconsistencies — do NOT propagate

### 1. `metadata` and `judge_outputs` JSON overlap

`SessionTurn.metadata` stores judge results in flattened legacy shape (for dashboard); `SessionTurn.judge_outputs` stores them in structured nested shape (for benchmark). Both updated per turn. **If adding a new judge:** write to `judge_outputs` only; only add to `metadata` if the teacher dashboard needs it.

### 2. `engine_state` JSON is untyped

No schema, no validation. Backward-compatibility hacks (mapping old `phase` to `session_state`) exist because of past key renames. **If adding new state:** at minimum, document the key in a docstring near where it's written. Better: use a Pydantic model for `engine_state` shape.

### 3. Backward-compat heuristics in identity

- `Course.is_math`: falls back to `MATH_KEYWORDS` heuristic when `subject_type` is unset.
- `LessonStep.priority` defaults to REQUIRED=1 for legacy steps that weren't reclassified.

These exist because schema changed and old data wasn't backfilled. **If introducing a new typed field:** write a backfill migration. Don't add a fallback heuristic and pretend it's done.

### 4. Vision judge dependencies are implicit

`run_all_judges()` accepts optional `vision_client`, `image_reader`, `attached_media` callbacks. If any are missing, `figure_vision` silently skips with no error. Callers must know to pass all three. **If adding a similar dependency:** require it in the signature or skip with a logged warning, never silently.

### 5. Multi-tenancy query pattern is duplicated

The `Q(inst) | Q(isnull=True)` pattern appears in 10+ views. **Don't add an 11th** — extract a manager method when you hit the 4th occurrence.

## When extending the codebase

**Decision tree.**

1. **Adding a new LLM provider?** → Subclass `BaseLLMClient`, add to factory dispatch. Pattern §1.
2. **Adding a new response evaluator?** → New file in `apps/tutoring/judges/`. Pattern §2. Add fields to `CombinedJudgeResult`.
3. **Adding a new curriculum entity?** → Decide where it fits in Course→Unit→Lesson→LessonStep. Add `priority` if it should be droppable. Pattern §3.
4. **Adding session-level state?** → Typed column on `TutorSession` if dashboard queries it; `engine_state` key if only the engine reads it. Document the key. Pattern §4.
5. **Adding user-facing data?** → Institution FK (nullable). Use the scoping query pattern. Pattern §5.
6. **Adding runtime-configurable behavior?** → ModelConfig Purpose enum value + DB row. Pattern §6.
7. **Adding a new Django app?** → Pick a place in the dependency stack. No circular imports. Pattern §7.

## Safety rules

❌ **Don't** add a JSONField without documenting its schema somewhere (docstring, README, or Pydantic model).
❌ **Don't** add cross-app circular imports. If you need to, the boundary is wrong.
❌ **Don't** hardcode model IDs in tutor/judge code — use `ModelConfig.get_for(purpose)`.
❌ **Don't** query user-facing data without the institution scoping filter.
❌ **Don't** put silent-skip behavior in dependencies — log warnings or require them.
❌ **Don't** add a 4th instance of a duplicated pattern without extracting it.

✅ **Do** mirror the existing pattern when adding a parallel component (new LLM provider, new judge, new step type).
✅ **Do** use the `Course → Unit → Lesson → LessonStep` hierarchy — don't introduce a parallel curriculum tree.
✅ **Do** store per-turn analysis on `SessionTurn.metadata` (legacy queries) or `judge_outputs` (benchmark).
✅ **Do** treat `Lesson.content_status` as the canonical generation state machine.
✅ **Do** verify changes work with `institution=None` (platform-wide) AND `institution=<specific>`.

## Further context

- `architecture-patterns-expert` skill — general principles to apply when choosing what to mirror
- `tutoring-engine-expert` skill — deep dive into `conversational_tutor.py` specifically
- `django-expert` skill — Django ORM / view patterns
- `CLAUDE.md` — critical rules (multi-tenancy, session state, media signaling)
- `memory/eval_benchmark_v2_simplified.md` — the benchmark format that drove `judge_outputs` design
