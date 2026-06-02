# Curriculum Parser v2 — Unified, Locale-Aware, LLM-Based

**Status**: approved (Edward's decisions inline in §6); ready to execute starting at M0.
**Owner**: Edward (eai6) + Claude
**Branch**: `feature/curriculum-parser-v2` (off `dev`)
**Trigger**: Mozambique pilot — first PT-language curriculum (Biologia 2º Ciclo, INDE/MEC) showed the existing parser drops to garbage on non-Seychelles documents.

### Decisions locked in (from Edward's review of §6)
- **Model**: Sonnet 4.6 for **both** the outline pass and the lessons pass. Maximum quality always — don't cost-optimise the parser path. (open Q1)
- **File layout**: Move existing `apps/curriculum/curriculum_parser.py` → `apps/curriculum/curriculum_parser_archive.py`. New v2 takes the canonical name `curriculum_parser.py`. Imports across the codebase keep working unchanged. (open Q3)
- **Old parser deletion**: Keep `curriculum_parser_archive.py` in tree until v2 has been tested against multiple countries/languages (PT-MZ, EN-SC at minimum; ideally one more pilot before deletion). M8 is **deferred** — not part of this plan. (open Q2)
- **Subject hint handling**: The teacher's subject dropdown is non-exhaustive ("General", "Biology", "Math", etc.), so don't treat it as ground truth. v2 passes the hint to the LLM as a **soft prior** alongside the document text and lets the LLM combine — if the LLM strongly disagrees, it wins; the disagreement is logged for audit. (open Q4)

---

## 1. Context — what we have today

### The current parser (`apps/curriculum/curriculum_parser.py`, 2566 lines)

Three parse paths, all sharing `extract_text_from_file` for OCR fallback:

| Function | Line | Role |
|---|---|---|
| `parse_curriculum_with_llm` | 1895 | Tries LLM first (primary path per `process_curriculum_upload:2437`) |
| `parse_mathematics_curriculum` | 1208 | Hand-rolled regex for Seychelles Math curriculum |
| `parse_geography_curriculum` | 1571 | Hand-rolled regex for Seychelles Geography syllabus |
| `parse_generic_curriculum` | 2073 | Bullet/header-pattern fallback for everything else |

The orchestrator at `process_curriculum_upload:2437` calls `parse_curriculum_with_llm` first, then falls through to subject-specific regex on exception.

### What we measured on the Mozambique Biology PDF (`mozambique/selected_materials/ANEXO_IX_PROGRAMA_BIOLOGIA_…pdf`, 76 pages)

**Ground truth**: 6 Unidades Temáticas across 3 grades (10ª/11ª/12ª Classe), 3 trimestres per grade, 3-column tables (`OBJECTIVOS ESPECÍFICOS | CONTEÚDOS | RESULTADOS DE APRENDIZAGEM`) + `Sugestões metodológicas` narrative per unit.

**Parser output**:

- `detect_subject(text, '')` → `'General'` (PT keywords absent from English-only keyword list at line 1164).
- `parse_curriculum_with_llm` → **crashes silently** with `TypeError: BaseLLMClient.generate() got an unexpected keyword argument 'prompt'`. The call at line 2001 uses `prompt=`, but the current signature is `generate(messages: list[dict], system_prompt: str, …)`. **This means every upload — Seychelles English included — has been silently falling through to regex since the LLM client refactor.**
- `parse_generic_curriculum` (the silent fallback) → **37 fake "units"** with titles like `"Impressão"` (the "Printing" line from the Ficha Técnica page), `"O aluno"` (every objectives column's heading), `"• Sistema Rh"` (a bullet line). None of the 6 real Unidades are correctly identified.

### Why this is the right time to fix it

- Mozambique pilot is the forcing function — Paschal's June visit needs a working PT upload flow.
- The silent LLM crash means **all current outputs are regex-only**, including the Seychelles courses already in prod. Fixing it improves the EN pipeline too.
- Tanzania pilot is queued behind Mozambique — adding a third country-specific regex parser would be wasted work.
- We already have `locale_prompts.py` from the M5-prep content-gen work — the locale-aware prompt machinery exists; we just need to plug it in.

---

## 2. Goals & non-goals

### Goals
- **One** parser path that handles any curriculum doc, in any locale we support, with the LLM doing the structural reasoning.
- **No regex for structure extraction.** Regex was used to compensate for "no AI"; we're going AI-first.
- **Locale-aware prompts** that adapt to the document's section terminology (e.g., "Unidade Temática" in PT, "Strand" in Seychelles English, "Mada" if/when Swahili materials show up).
- **Loud failures.** When extraction fails, the user sees what went wrong — no silent fallthrough to a worse path.
- **Idempotent + observable.** Re-running on the same doc produces the same structure; the upload log shows what the LLM saw and what it produced.

### Non-goals
- **Not building a "fact extractor" yet.** This is structure (Course → Unit → Lesson → objectives); content stays the job of `content_generator.py`.
- **Not replacing the OCR/text-extraction layer.** `extract_text_from_file` and the vision OCR fallback stay as-is — they're orthogonal and already work.
- **Not fixing figure extraction.** Out of scope; `extract_figures_from_pdf` is separate.

---

## 3. Research / approach

### LLM-first structured extraction — current state of the art

- **Structured outputs (constrained decoding)** beat "ask for JSON in prose" by 30–40 % on schema adherence for extraction tasks (Tam et al. 2025; OpenAI Structured Outputs / Anthropic `output_config.format` / Gemini `response_schema`). For a 5-field unit-lesson schema this is a clear win — we get guaranteed-shape output without a retry loop.
- **Query-last for long context** (Anthropic long-context tips): document goes first, instructions and schema go last. ~30 % quality improvement on multi-doc extraction. This matters because curriculum PDFs are 50–100 pages.
- **Two-pass for very long docs**: pass 1 produces the **outline** (units only); pass 2 expands each unit's lessons. Avoids the model hand-waving on the back half of the document. For a 76-page Biology doc with the table-per-unit format, this is the right shape.
- **Don't ask for everything in one JSON** — Anthropic's own guidance: split structure (units/lessons) from prose (descriptions, methodological suggestions). We can fetch prose with a follow-up call where useful, OR drop it entirely since `content_generator.py` re-generates it later.

### Locale awareness — what `locale_prompts.py` already gives us
- A `locale_instruction_block(locale)` helper that returns an XML `<locale>` block for `pt-mz` (tu informal, Acordo Ortográfico 1990, MZ vocab/place-examples). Returns `""` for `en-us` so the EN cache key stays stable.
- The block currently targets *generation* prompts. For *parsing*, we want a different shape: tell the model "the document is in `<locale>`; use that to recognise terminology like 'Unidade Temática', 'Trimestre', 'Classe' as structural cues". This is a new function in the same module: `locale_parser_hints(locale)`.

### Vendor-agnostic — use the existing `BaseLLMClient`
- `ModelConfig.get_for('generation')` already picks Claude per the project's "Generation = Claude" rule (`memory/auto-memory/project_model_routing.md`).
- **Sonnet 4.6 everywhere** (both outline + lessons passes). No cost-optimisation in this path — parser correctness is too load-bearing to compromise on model quality. Locked per Edward's review.

---

## 4. Architecture

### Module layout — archive-and-replace

- **Move**: `apps/curriculum/curriculum_parser.py` → `apps/curriculum/curriculum_parser_archive.py` in one renaming commit at M0. Everything in the archive file stays callable but is **dormant** — nothing imports from it after M5.
- **New v2 takes the canonical name** `apps/curriculum/curriculum_parser.py` — single entry point, ~300–400 lines, locale-aware, LLM-only.
- **Why this layout** (Edward's decision): callers across the codebase (`apps/dashboard/views.py`, `apps/curriculum/pipeline.py`, tests, management commands) import from `apps.curriculum.curriculum_parser` — keeping that import path means no churn at integration time. The archive file is purely a safety net we can rip out once v2 is validated against multiple country/language datasets.
- **Re-exports for compat**: the new `curriculum_parser.py` re-exports the still-needed text-extraction helpers (`extract_text_from_file`, `extract_from_pdf`, `extract_from_docx`, `extract_from_image`, `extract_figures_from_pdf`, `_strip_nul`, the OCR vision functions, `OCRFailure`) from the archive module so existing call sites continue to work. Only the structure-extraction functions are replaced.

### Public API
```python
def parse_curriculum(
    file_path: str,
    *,
    subject_hint: str = '',          # teacher-provided ("Biology"), optional
    grade_hint: str = '',             # teacher-provided ("10ª Classe"), optional
    locale: str = 'en-us',            # from CurriculumUpload.locale
    institution_id: int | None = None,
    progress_cb: Callable | None = None,
) -> ParsedCurriculum:
    """Single entry point. Always uses the LLM. Raises ParseFailure
    with a structured reason on failure — never silently returns
    garbage."""
```

### Data flow

```
file_path
  │
  ├─► extract_text_from_file  ──►  raw_text (existing, vision OCR fallback intact)
  │
  ├─► detect_subject_and_locale  ──►  (subject, locale, grade_range)
  │   ▸ LLM-based, NOT keyword matching. One short call: "look at the
  │     first 2K chars, return JSON {subject, locale, grade_range}".
  │     Replaces the broken English-keyword `detect_subject`.
  │   ▸ Takes the teacher's subject_hint as a SOFT PRIOR — the LLM is
  │     told "the teacher suggested 'X' but trust the document". If the
  │     LLM picks something different, the upload log records the
  │     disagreement (hint=X, detected=Y) so we can audit later.
  │     Decision: the LLM wins. The dropdown is non-exhaustive on the
  │     upload form and teachers may pick the wrong bucket.
  │
  ├─► outline_pass  ──►  list[UnitOutline]
  │   ▸ "Given this curriculum doc, return the high-level units only —
  │     title + 1-line description + grade. No lessons yet."
  │   ▸ Structured output via Anthropic output_config.format.
  │
  └─► for each UnitOutline: lessons_pass  ──►  list[Lesson]
      ▸ Pass JUST this unit's text excerpt + outline context.
      ▸ "Return lessons for this unit: title, objective, enabling_objectives."
      ▸ Structured output.
      ▸ Parallel-fanout via ThreadPoolExecutor with bounded concurrency
        (3–5 workers — same pattern as judges/orchestrator).
```

### Locale-aware prompt construction

Reuse `locale_prompts.py`:

```python
def locale_parser_hints(locale: str) -> str:
    """Return parser-specific locale guidance, distinct from
    locale_instruction_block (which is for content GENERATION)."""
```

For `pt-mz` this returns hints like:
> The document is in Mozambique Portuguese.
> Section terminology you may see:
> - "Unidade Temática" = unit
> - "Conteúdos" = topic/content list
> - "Objectivos Específicos" = enabling objectives
> - "Resultados de Aprendizagem" = learning outcomes / lesson objectives
> - "Trimestre" = academic term
> - "10ª/11ª/12ª Classe" = grade levels (do not coerce to S1/S5)
> Treat objective verbs in PT (`interpretar, descrever, identificar, mencionar, explicar, aplicar, relacionar, comparar, distinguir`) as enabling-objective cues.

For `en-us` (Seychelles) we return the existing-style hints unchanged so the EN pipeline output is byte-stable.

### Schema

```python
class Lesson(BaseModel):
    title: str
    objective: str                       # terminal: "what the student does"
    enabling_objectives: list[str] = []  # granular sub-steps
    order: int

class UnitOutline(BaseModel):
    title: str
    grade_level: str                     # "10ª Classe" / "S3" / etc.
    description: str = ""
    page_range: tuple[int, int] | None = None  # for the lessons_pass

class ParsedCurriculum(BaseModel):
    subject: str
    locale: str
    grade_levels: list[str]              # ["10ª Classe", "11ª Classe", "12ª Classe"]
    cycle: str = ""
    description: str = ""
    units: list[Unit]                    # Unit = UnitOutline + lessons
```

### Failure model

`ParseFailure` exception with stable `reason` slugs (mirroring `OCRFailure`):
- `no_text` — extraction returned <100 chars (PDF unreadable)
- `subject_unclassified` — locale+subject detection had no clear winner
- `no_units_found` — outline pass returned 0 units
- `lesson_pass_failed` — every unit's lesson pass crashed
- `llm_unavailable` — `ModelConfig.get_for('generation')` returned None
- `llm_error` — provider-level error (rate limit, 4xx)

The upload UI surfaces the reason slug + the LLM's raw output preview so we can debug from logs.

### Wiring into `process_curriculum_upload`

`apps/curriculum/curriculum_parser.py::process_curriculum_upload:2437` changes from:

```python
curriculum = parse_curriculum_with_llm(text, det, …)   # broken
# regex fallback chain
```

To:

```python
from apps.curriculum.curriculum_parser_v2 import parse_curriculum, ParseFailure
try:
    curriculum = parse_curriculum(
        upload.file_path,
        subject_hint=upload.subject_name,
        grade_hint=upload.grade_level,
        locale=upload.locale,
        institution_id=upload.institution_id,
    )
except ParseFailure as e:
    upload.add_log(f"❌ Parse failed: {e.reason} — {e.detail}")
    upload.status = 'failed'
    raise
```

No regex fallback. Upload goes to `review` with an actionable error message.

---

## 5. Milestones & deliverables

### **M0 — branch + archive rename + baseline snapshot** (45 min)
- New branch `feature/curriculum-parser-v2` off `dev`.
- **Rename**: `git mv apps/curriculum/curriculum_parser.py apps/curriculum/curriculum_parser_archive.py`. Single rename commit, no content changes. All current callers of `from apps.curriculum.curriculum_parser import …` BREAK at this point — fix them by adding a thin shim `curriculum_parser.py` that re-exports from `curriculum_parser_archive` (just the extraction helpers — the structure-parsing functions go away at M5).
- Snapshot current parser output on the 3 Seychelles courses already in prod (Belonie Geography, Geography S1-S5, Math S3) + the Mozambique Biology doc so we have a baseline diff target.
- **Deliverable**: branch + 1 rename commit + 1 shim commit + `memory/parser_v2_baseline.json` with existing-parser output for 4 docs.

### **M1 — fix the silent LLM crash in the archive module** (45 min)
- Patch `curriculum_parser_archive.parse_curriculum_with_llm` to use the correct `BaseLLMClient.generate(messages=[{"role":"user","content":prompt}], system_prompt=…)` signature AND handle the `LLMResponse` dataclass (`.content` attribute, not `.get('content')`).
- Confirm the LLM path actually runs on a Seychelles doc end-to-end.
- **NOT** the final fix — just stops the bleed so the M0 baseline captures what the archive *would* have produced if it worked, not the silently-broken regex output. Helps M6 regression diff.
- **Deliverable**: 1 commit. Re-run M0 baseline after this patch lands so the diff is meaningful.

### **M2 — v2 module scaffold + LLM client wrapper** (2 hr)
- Create `apps/curriculum/curriculum_parser.py` (the new v2 — replaces the shim from M0) with the public API in §4.
- Internal `_call_llm_structured(system, user, schema)` that uses `BaseLLMClient.generate` (Sonnet 4.6) + Anthropic `output_config.format` for guaranteed schema. (For non-Anthropic providers we fall back to "parse JSON from text", same pattern as `content_generator._try_fix_json`.)
- Subject + locale detection via LLM, returning `(subject, locale, grade_range)` — soft-prior on teacher hint.
- Re-export the extraction helpers from `curriculum_parser_archive` so existing imports keep working.
- **Deliverable**: empty pipeline runs without errors; subject detection on `bio.pdf` returns `("Biology", "pt-mz", ["10ª Classe","11ª Classe","12ª Classe"])` instead of `"General"`.

### **M3 — outline pass + locale hints** (2 hr)
- Implement `outline_pass(text, subject, locale)` — single LLM call (Sonnet 4.6), query-last layout, returns `list[UnitOutline]`.
- Add `locale_parser_hints(locale)` to `locale_prompts.py` (new function, distinct from `locale_instruction_block`).
- Constrain output via structured-output schema.
- **Deliverable**: on the Mozambique Biology PDF, outline_pass returns exactly the **6 real Unidades Temáticas** with their grade assignments (Citologia → 10ª; Genética → 10ª/11ª; etc.). On the Seychelles Geography PDF it returns the existing unit count ±1.

### **M4 — lessons pass + parallel fanout** (3 hr)
- Implement `lessons_pass(unit_outline, full_text)` — extracts lessons for one unit at a time (Sonnet 4.6), using the unit's title and (where present) page hints to slice the relevant excerpt before sending to the LLM.
- ThreadPoolExecutor with 3 workers + 30s per-unit timeout. Same fail-soft pattern as `judges/__init__.py::run_all_judges` (per-unit exceptions don't kill the whole parse).
- **Deliverable**: full parse of the Mozambique Biology PDF produces a structure roughly matching the table-of-contents — 6 units, ~30–40 lessons total. Side-by-side diff with the actual TOC included in the PR description.

### **M5 — replace the orchestrator path + ParseFailure surfacing** (1 hr)
- `process_curriculum_upload` (which lives in the archive module today) moves to v2 and calls `parse_curriculum()`; archive's regex-fallback chain (mathematics + geography + generic) no longer reachable from production.
- `ParseFailure` reasons surface in the upload UI log + status.
- Archive module stays compiled and importable as a safety net but nothing in the runtime path touches it after this milestone.
- **Deliverable**: upload UI shows the new path running. A deliberately-broken PDF (e.g., a blank file) shows a clean `no_text` error in the upload log, not a stack trace.

### **M6 — regression test the 3 Seychelles prod courses** (1.5 hr)
- Run v2 against the 3 prod courses (Belonie Geography, Geography S1-S5, Math S3). Diff against the M0+M1 baseline (the M1 patch makes that diff meaningful — pre-M1, baseline was silently broken).
- Compare against what a teacher would *expect* — Edward or a pilot teacher eyeballs the diff.
- Fix any prompt issues that cause regressions.
- **Deliverable**: side-by-side diff report in `memory/parser_v2_seychelles_regression.md`. Sign-off from Edward before M7.

### **M7 — chrome-devtools-mcp E2E on local + staging** (1 hr)
- Run the full upload flow locally with `bio.pdf`. Watch the upload log update with the new step names. Approve the parsed structure via the review UI. Confirm Course → Unit → Lesson rows land in Postgres with correct PT titles.
- Same flow on `staging.seselai.sc` after merge to `dev`.
- **Deliverable**: screenshots in the commit/PR; clean staging deploy.

### **M8 — archive deletion** (DEFERRED — not in this plan)
- Per Edward's decision, the archive module stays in tree until v2 has been validated against multiple country/language datasets (PT-MZ + EN-SC at minimum; ideally TZ Swahili or one more pilot before deletion).
- When deletion is approved, it will be a separate single-commit cleanup that removes `curriculum_parser_archive.py` and drops the re-export shim.
- **Out of scope for this plan.** Re-open as `memory/curriculum_parser_archive_deletion.md` when ready.

---

## 6. Risks & open questions

### Risks

- **Cost.** Two-pass extraction with parallel fanout on 100-page docs could be 6–12 LLM calls per upload at ~30K tokens each. At Claude Sonnet 4.6 prices that's roughly $0.50–$1.00 per upload. **Mitigation**: aggressive prompt caching of the system prompt + locale hints (these are the static prefix); cheap model (Haiku 4.5) for outline + subject detection where the constraint is tight; only Sonnet/Opus for the lesson-extraction pass where accuracy matters. Budget under $0.30 per upload at p50.

- **Latency.** Current upload review path is ~30s end-to-end; the two-pass + fanout could push it to 60–90s. **Mitigation**: the existing UI already shows live progress via `upload.add_log`; we'll wire `progress_cb` to update per-unit. Users have already accepted ~minutes-long content generation; structure extraction at ~60s is fine.

- **LLM hallucinated lessons that aren't in the doc.** Especially in the lessons-pass where we pass excerpts. **Mitigation**: require each lesson to include a `source_evidence` field (verbatim quote from the doc); reject lessons whose evidence isn't actually in the text via post-validation. Same anti-hallucination pattern as Anthropic's "quote-then-answer" guideline.

- **The teacher review UI assumes a roughly-correct structure.** If v2 produces dramatically different unit counts on prod courses, teachers may not realise they need to re-review. **Mitigation**: M6 regression report is non-negotiable; we get explicit sign-off before retiring v1.

### Resolved decisions (from Edward's inline review — kept here as a record)

1. **Model choice**: Sonnet 4.6 for **both** outline + lessons passes. No Haiku optimisation. Maximum quality always. Recorded in §3 and locked into M2–M4.
2. **Old parser retention**: `curriculum_parser_archive.py` stays in tree until v2 has been validated across multiple countries/languages. M8 (deletion) deferred — separate plan to be opened later.
3. **File layout**: Archive-and-replace — move existing file to `curriculum_parser_archive.py`, new v2 takes the canonical `curriculum_parser.py` name. No churn at import sites. M0 starts with this rename.
4. **Subject hint precedence**: Teacher's dropdown is a **soft prior**, not ground truth. The LLM sees the hint as "the teacher suggested X" and is free to disagree based on the document text. Disagreements logged for audit. Wired in M2's `detect_subject_and_locale`.

---

## 7. Sequencing & dependencies

```
M0 archive+baseline ──► M1 unblock LLM in archive ──► M2 v2 scaffold ──► M3 outline ──► M4 lessons ──► M5 orchestrator ──► M6 regression ──► [Edward sign-off] ──► M7 E2E
                                                                                                                                                                  │
                                                                                                                                                                  └─► [later] M8 archive deletion (deferred, separate plan)
```

M0 ships to `dev` immediately — pure rename + shim, no behaviour change. M1 ships next as a pure bug fix in the now-renamed archive. M2–M7 ship together to `dev` after M6 regression passes.

Estimated total time: **~11 hours of focused work** spread across 2–3 days (M8 excluded — deferred).

---

## 8. Out-of-scope follow-ups (not this plan, but worth noting)

- The `parse_curriculum_with_llm` crash means the **regex parsers have been running on every Seychelles upload too**. After M1 lands, the LLM path will start producing *different* output for existing Seychelles courses than what's currently in prod DB. Need to decide: re-parse existing courses, or leave them and apply v2 only to new uploads? **Recommend leave-as-is** — re-parsing live content could surprise teachers; v2 applies to net-new uploads only.

- Multi-grade documents (the Mozambique Biology PDF covers 10ª/11ª/12ª) should produce **separate Course rows per grade**, not one mega-course. The current data model supports this (`Course.grade_level` is a single value); v2 needs to fan out at the `create_curriculum_from_structure` step. Folded into M4.

- Tanzania pilot will add Swahili — `locale_parser_hints('sw-tz')` and an addition to the locale dropdown. Trivial extension once v2 is in.

---

## Citations to existing memory

- `memory/portuguese_mozambique_pilot_plan.md` — the M5-prep work for content gen that produced `locale_prompts.py`; this plan extends the same locale model to parsing.
- `memory/auto-memory/project_model_routing.md` — "Generation = Claude" decision; parser inherits this choice.
- `memory/auto-memory/feedback_verify_rendered_templates_before_push.md` — the chrome-devtools verification habit; M7 follows it.
- `memory/eval_benchmark_v2_simplified.md` — tutor eval benchmark; once v2 lands, we should add a parser-output benchmark slice (out of scope here but flagged).
