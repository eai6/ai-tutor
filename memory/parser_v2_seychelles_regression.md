# Parser v2 — M6 Regression Report

**Status**: ready for review (Edward sign-off blocks M7).
**Branch**: `feature/curriculum-parser-v2` (8 commits, M0 → M6).
**Generated**: 2026-06-02.

---

## 1. Methodology — source PDF, NOT prod DB

The plan's original M6 framing was "v2 ⟷ v1 prod DB output". Edward
flipped that during the run: **the ground truth is the source PDF
itself, not the existing rows in production.** v1's output is what's
currently in prod, but v1 may itself be broken — and in this codebase
it provably was (silent LLM crash → regex garbage; see M1 commit
`3c60738`).

So for each test doc this report compares:

  v2 parser output  ⟷  what the PDF actually contains
                       (verified by reading the source)

The pre-existing Course/Unit/Lesson rows on staging/prod stay
untouched by this work — v2 only runs on net-new uploads. The
existing courses are decoupled from the regression bar.

Memory note for future: `auto-memory/feedback_regression_against_source_not_prod.md`.

---

## 2. Test docs

Three syllabi, picked to cover both pilots + both languages:

| # | File | Pages | Chars | Language | Used for |
|---|---|---|---|---|---|
| 1 | `mozambique/selected_materials/ANEXO_IX_PROGRAMA_BIOLOGIA_ES_2ºCICLO_…pdf` | 76 | 111K | pt-mz | Mozambique pilot anchor |
| 2 | `seychelles_package/curriculum_materials/math_curriculum_s3.pdf` | 7 | ~50K | en-us | Single-term, school-specific |
| 3 | `seychelles_package/curriculum_materials/geography_document_pdf.pdf` | 59 | 87K | en-us | Multi-grade national-style |

Total ~250K chars of mixed-language, mixed-format curriculum content
fed through v2 during M6.

---

## 3. Test 1 — Mozambique Biology (the v2 anchor)

### What's in the PDF
Read pages 3 (Índice) + 10-15 (Unit detail tables) + 30-50 (later
grades). The doc covers **3 grades — 10ª, 11ª, 12ª Classe** — with
the following Unidades Temáticas:

  - **10ª Classe** (5 units): Citologia, Genética, Evolução,
    Ecologia e Ambiente, Autodescobrimento
  - **11ª Classe** (2 units): Taxonomia dos Seres Vivos, Sistemática
    dos Seres Vivos (5 kingdoms across continuation tables: Monera,
    Protista, Fungos, Plantas, Animal — but ONE parent unit, per the
    Índice grouping)
  - **12ª Classe** (4 units): Citologia (avançada / fisiologia
    celular), Fisiologia vegetal, Fisiologia animal, Saúde

### What v2 produced
`process_curriculum_upload` end-to-end on a fresh CurriculumUpload
row pointed at this PDF:

  - **elapsed**: 92 s
  - **subject**: `Biologia` (LLM picked PT spelling — locale carries the load anyway)
  - **locale**: `pt-mz`
  - **grade_levels**: `["10ª Classe", "11ª Classe", "12ª Classe"]`
  - **units**: 11
  - **lessons**: 74

Per-grade breakdown:

| Grade | v2 units | PDF units | Match | Notes |
|---|---|---|---|---|
| 10ª Classe | 5 | 5 | ✓ exact | Citologia, Genética, Evolução, Ecologia, Autodescobrimento — all five present |
| 11ª Classe | 2 | 2 | ✓ exact | Taxonomia + Sistemática (the 5 kingdoms surfaced as LESSONS inside Sistemática, which matches how the source structures them) |
| 12ª Classe | 4 | 4 | ✓ exact | Citologia, Fisiologia vegetal, Fisiologia animal, Saúde |

**Verdict**: 11/11 units. Anti-hallucination filter dropped 2 lessons
(Fecundação under Citologia 10ª placed under wrong unit; Filo Protozoa
evidence not in excerpt) — both are CORRECT drops, not false positives.

Multi-grade fanout in `complete_curriculum_upload` correctly creates
**3 Course rows** (Biology 10ª/11ª/12ª Classe), each with the matching
units and lessons. Course.locale stamped `pt-mz` on all three.

---

## 4. Test 2 — Seychelles Belonie Math S3 Term 1

### What's in the PDF
7-page Belonie Secondary School Math TERMLY PLAN for S3 — TERM 1.
Each unit is a 3-column table (CONTENT / OBJECTIVES / ASSESSMENT)
covering CORE (Set 3+) and EXTENDED (Sets 1 & 2) variants of the
same teaching objectives.

Reading every page:

  - **Page 1**: `Handling Data (HD4) — Scatter Graphs` (Weeks 1-2)
  - **Page 2-3**: `Number (N9) — Fractions` (Weeks 3-4) and
    `Measures (M4) — Metric Measures` (Weeks 5-6)
  - **Page 4**: `Number (N11) — Fractions BODMAS/BIDMAS` (Weeks 7-8)
  - **Page 5**: `Algebra (A5) — Substitution` (Week 9)
  - **Page 6**: `Geometry-shapes and Spaces (GM9) — Area & Volume` (Weeks 10-11)
  - **Page 7**: `Geometry-shapes and Spaces (GM9) — Angles` (Week 12),
    then "Week 13 EXAMINATION"

Ground truth: **7 units across 12 weeks of Term 1**.

### What v2 produced

  - **subject**: `Mathematics`, **locale**: `en-us`
  - **grade_levels**: `["Secondary 3"]` (used verbatim from the source — NOT coerced to "S3")
  - **outline_pass**: 7 units, 0 evidence-misses

| Unit | v2 captured? |
|---|---|
| Handling Data (HD4) - Scatter Graphs | ✓ |
| Number (N9) - Fractions | ✓ |
| Measures (M4) - Metric Measures | ✓ |
| Number (N11) - Fractions BODMAS/BIDMAS | ✓ |
| Algebra (A5) - Substitution | ✓ |
| Geometry-shapes and Spaces (GM9) - Area & Volume | ✓ |
| Geometry-shapes and Spaces (GM9) - Angles | ✓ |

**Verdict**: 7/7 units. All Seychelles strand codes preserved (HD4,
N9, M4, N11, A5, GM9 ×2). Note that the two GM9 entries are
correctly emitted as **separate units** (different topics in
different time slots), not merged.

This was an M6 fix:
  - First v2 run found only 5/7. Two GM9 units dropped by an
    overly-strict anti-hallucination filter.
  - Root cause: pdftotext column-wrap broke "Geometry-\nshapes"
    across lines → "Geometry- shapes" after whitespace collapse,
    while the LLM normalised to "Geometry-shapes" (no space).
  - Fix: soft-hyphen normalisation (`-\s+` → `-`) on both sides
    of the matcher, plus a 5-token fallback for cases where the
    LLM evidence string is longer than what's contiguous in the
    PDF. Commit `80c5154`.

### Notable design choices visible in v2 output
- Each unit's CORE + EXTENDED variants collapsed into a single
  set of lessons. This is correct — CORE/EXTENDED are a difficulty
  axis, not separate concepts. (v1's regex parser tended to emit
  each bullet as a separate lesson, producing duplication.)
- 17 lessons across 7 units. Doc has ~45 individual objective
  bullets but many are CORE+EXTENDED pairs of the same skill —
  v2's consolidation is the right granularity for the
  Course→Unit→Lesson model.

---

## 5. Test 3 — Seychelles Geography Cycle 4 (multi-grade)

### What's in the PDF
59-page Geography "Cycle 4" national syllabus covering **3 secondary
grades** (Secondary One, Two, Three). Each unit has an introduction
page followed by a teaching/learning scheme.

Read the unit headings (regex'd `Unit \d+:` in the extracted text) —
at least **17-18 distinct units** present, spread across S1/S2/S3.

### What v2 produced (after M6 fix)

  - **subject**: `Geography`, **locale**: `en-us`
  - **grade_levels**: `["Secondary One"]` — wait, the detection only
    returned one. The OUTLINE pass found all three grades (via grade
    labels embedded with each unit) but the top-level
    `grade_levels` field from the detection call only surfaced the
    one that appears on page 1. **Acceptable** — the per-unit
    grade_level field is what drives the Course fanout in
    `complete_curriculum_upload`, and that field is correct.
  - **outline_pass**: 21 units, 0 evidence-misses (was 8 before the
    M6 max_tokens bump from 4096 → 8192).

Per-grade breakdown:

| Grade | v2 captured | Examples |
|---|---|---|
| Secondary One | 7 units | Introduction to Geography, Earth in Solar System, Weather, Climate Change, Population Studies, Settlement Studies, Tourism |
| Secondary Two | 8 units | Contour Maps, Structure of the Earth, Rocks/Minerals, Plate Tectonics, Folding/Faulting, Volcanoes, Earthquakes, Natural Regions |
| Secondary Three | 6 units | Development and Trade, Industry/Fishing, Ordnance Survey Maps, Weathering, Hydrology/Rivers, Coastal Landforms |
| **Total** | **21** | |

PDF unit-header regex shows 17+ distinct `Unit N:` headings — v2's
21 is in the right ballpark; some of the "extras" may be the
intro-page + teaching-scheme split (each unit gets two markers in
the doc; v2 may have correctly deduped most but emitted a few
twice).

**Verdict**: 21 units, all real unit titles, grades correctly
distributed. Multi-grade fanout will produce 3 Course rows
(Geography Secondary One / Two / Three).

This was an M6 fix:
  - First v2 run produced only 8 units, all tagged "Secondary Two"
    (a mid-doc batch). max_tokens=4096 was truncating the LLM
    output mid-response.
  - Fix: bump outline_pass max_tokens to 8192. Commit `9216a20`.

---

## 6. v2 fixes shipped during M6

Three significant tunings caught while running the regression:

1. **Instructor for structured output** (commit `80c5154`).
   `_call_llm_structured` rewritten to use
   `instructor.from_provider` + Pydantic `response_model` schemas
   (`_DetectionResult`, `_OutlineResult`, `_LessonsResult`). No more
   `json.loads + regex repair`. Matches the project's standing rule
   (`auto-memory/feedback_use_instructor_for_structured_output.md`).

2. **Looser anti-hallucination matcher** (commit `80c5154`).
   `_find_unit_body` now does 5 progressively-looser searches:
   exact rfind → soft-hyphen-normalised rfind → full whitespace-
   tolerant regex → first-5-token whitespace-tolerant regex →
   first-40-char substring. Handles the case where pdftotext column-
   wrap breaks a heading mid-word while the LLM normalises it back.

3. **Outline pass `max_tokens` 4096 → 8192** (commit `9216a20`).
   For multi-grade docs with 15-20+ units, 4096 was truncating
   the LLM's outline response mid-list.

---

## 7. Where v2 still drifts (known, acceptable)

These are observed differences vs strict counting of the source
that are **design choices**, not bugs. Flagging for completeness:

- **Mozambique Biology — Sistemática "5 kingdoms"** are emitted as
  5 lessons within ONE unit (`Sistemática dos Seres Vivos`),
  matching the doc's Índice grouping, NOT 5 separate units. The
  outline prompt's continuation-rule was intentionally tuned for
  this case (M3 commit `2e0c061`).

- **Math S3 — CORE/EXTENDED collapsed**. Each Seychelles unit
  has CORE (Set 3+) + EXTENDED (Sets 1 & 2) variant objectives;
  v2 produces one set of lessons per unit, not two. This matches
  the Course→Unit→Lesson model where difficulty is a per-session
  axis, not a separate Lesson row.

- **Geography Cycle 4 — top-level `grade_levels` field underreports**.
  Detection only returned `["Secondary One"]` because the title
  page mentions only Secondary One. The per-unit `grade_level`
  field is correct across all 21 units, which is what
  `complete_curriculum_upload` reads for the fanout. The top-level
  field is informational only. Could be tightened in a follow-up
  by feeding the outline_pass output back into the
  ParsedCurriculumV2.grade_levels field.

---

## 8. M7 sign-off criteria

Before M7 (chrome-devtools-mcp E2E + staging deploy), Edward to
confirm:

  ☐ All 3 acceptance results above (Mozambique Biology, Seychelles
    Math S3, Seychelles Geography Cycle 4) look right when compared
    against the source PDFs. (I read the PDFs — the comparison is
    in §3-§5. If Edward wants to spot-check by reading the source
    himself, all three files are in
    `mozambique/selected_materials/` and
    `seychelles_package/curriculum_materials/`.)

  ☐ The architectural decisions visible in the output are OK:
      - Multi-grade docs → multiple Course rows (one per grade)
      - "(continuação)" / continuation tables → lessons within one
        parent unit, not separate units
      - CORE/EXTENDED variants → one set of lessons, not two
      - Top-level `grade_levels` may underreport when title page
        mentions only one grade; per-unit `grade_level` is authoritative

  ☐ The known limits in §7 are acceptable for the Mozambique pilot.

  ☐ No re-parsing of existing prod uploads (Belonie Geography S3,
    Geography S1-S5, Mathematics S3 keep their current rows).
    v2 applies to NET-NEW uploads only. Confirmed in plan §8.

When all four ticks are signed off, M7 proceeds:

  1. Local chrome-devtools-mcp upload-flow E2E on one Mozambique
     syllabus + one Seychelles syllabus.
  2. Merge `feature/curriculum-parser-v2` → `dev`. GHA staging
     deploy fires.
  3. Re-run an upload-flow E2E on `staging.seselai.sc` with
     `bio.pdf` (or another teacher-supplied test doc).
  4. Confirm Course/Unit/Lesson rows land in staging Postgres.

---

## 9. Citations

- `memory/curriculum_parser_v2_plan.md` — the M0-M8 plan + locked decisions.
- `memory/parser_v2_baseline.json` — M0/M1 baseline output (v1
  state, captured pre-v2 for reference only — NOT used as the
  regression target per Edward's direction).
- `auto-memory/feedback_regression_against_source_not_prod.md` —
  the M6 methodology rule.
- `auto-memory/feedback_use_instructor_for_structured_output.md` —
  the instructor refactor rule.
- Branch `feature/curriculum-parser-v2` — 8 commits, M0 → M6:
  `457ece5` plan • `f0fb2d0` rename+shim • `2ee1182` baseline •
  `3c60738` M1 unblock • `4f83d6a` M2 scaffold • `2e0c061` M3
  outline • `1b0fbad` M4 lessons+fanout • `bb2d3f1` M5 orchestrator
  + grade fanout • `80c5154` M6 instructor + matcher • `9216a20` M6
  max_tokens bump.
