"""Extract a small slice of curriculum from prod_content_dump.sql into Django fixtures.

The eval harness references lessons by ID. To keep eval results comparable
across runs we freeze a small set of lessons here as a Django fixture, then
load them into the dev/test DB via ``manage.py loaddata``.

Usage:
    python evals/fixtures/extract.py [--dump <path>] [--lessons <N>]

Outputs:
    evals/fixtures/institution.json   # 1 Institution + 1 simulator-bot User
    evals/fixtures/lessons.json       # Course → Unit → Lesson → LessonStep + ExitTicket(s)

The script:
1. Parses the PostgreSQL COPY blocks for the relevant tables.
2. Picks lessons in 'ready' state that belong to published math/geography courses.
3. Walks the FK graph (course → unit → lesson → step + exit ticket + questions).
4. Reparents extracted courses to the synthetic eval Institution
   (PK=999001). User FKs (teacher_approved_by, reviewed_by) are nulled.

Preserves original PKs from the dump. Collisions with locally-created dev
content are the operator's responsibility — drop the relevant rows or wipe
the dev DB before loaddata if you hit one.

See memory/eval_harness_plan.md.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Synthetic eval-only Institution. High ID to avoid collisions with real
# institutions in any dev DB.
EVAL_INSTITUTION_PK = 999001
EVAL_USER_PK = 999001

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DUMP = REPO_ROOT / 'prod_content_dump.sql'

# Tables we care about, in dependency order.
TABLES = [
    'curriculum_course',
    'curriculum_unit',
    'curriculum_lesson',
    'curriculum_lessonstep',
    'tutoring_exitticket',
    'tutoring_exitticketquestion',
]

# Column definitions parallel to the COPY headers in the dump.
# Each entry: (django_model_label, json_columns, bool_columns)
TABLE_META = {
    'curriculum_course': (
        'curriculum.course',
        {'grade_levels'},
        {'is_published', 'allow_student_duration_override', 'tutoring_images_enabled', 'prerequisites_enabled'},
    ),
    'curriculum_unit': (
        'curriculum.unit',
        {'enabling_objectives', 'terminal_objectives'},
        set(),
    ),
    'curriculum_lesson': (
        'curriculum.lesson',
        {'metadata', 'enabling_objectives'},
        {'is_published', 'teacher_approved', 'allow_group_mode', 'group_requires_approval'},
    ),
    'curriculum_lessonstep': (
        'curriculum.lessonstep',
        {'choices', 'media', 'educational_content', 'curriculum_context', 'judge_outputs'},
        set(),
    ),
    'tutoring_exitticket': (
        'tutoring.exitticket',
        set(),
        {'is_published'},
    ),
    'tutoring_exitticketquestion': (
        'tutoring.exitticketquestion',
        {'answer_data', 'template_data', 'judge_outputs'},
        set(),
    ),
}

# Fields renamed between the SQL column name and the Django field name (FK
# columns: SQL has `*_id`, Django fixture wants the unsuffixed field name).
FK_RENAMES = {
    'institution_id': 'institution',
    'curriculum_upload_id': 'curriculum_upload',
    'course_id': 'course',
    'unit_id': 'unit',
    'lesson_id': 'lesson',
    'exit_ticket_id': 'exit_ticket',
    'teacher_approved_by_id': 'teacher_approved_by',
    'reviewed_by_id': 'reviewed_by',
}

# FKs to null out on extract (we don't drag along these foreign tables).
NULL_OUT_FKS = {'curriculum_upload', 'teacher_approved_by', 'reviewed_by'}


def unescape_pg(s: str) -> str | None:
    """Reverse PostgreSQL text-format COPY escaping."""
    if s == r'\N':
        return None
    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == '\\':
                out.append('\\'); i += 2
            elif nxt == 'n':
                out.append('\n'); i += 2
            elif nxt == 't':
                out.append('\t'); i += 2
            elif nxt == 'r':
                out.append('\r'); i += 2
            elif nxt == 'b':
                out.append('\b'); i += 2
            elif nxt == 'f':
                out.append('\f'); i += 2
            else:
                out.append(s[i]); i += 1
        else:
            out.append(s[i])
            i += 1
    return ''.join(out)


def parse_copy_blocks(sql_text: str) -> dict[str, list[dict]]:
    """Return {table_name: [row_dict, ...]} for tables we care about."""
    rows: dict[str, list[dict]] = {t: [] for t in TABLES}
    pattern = re.compile(
        r'COPY public\.(\w+) \(([^)]+)\) FROM stdin;\n(.*?)\n\\\.',
        re.DOTALL,
    )
    for match in pattern.finditer(sql_text):
        table = match.group(1)
        if table not in rows:
            continue
        columns = [c.strip() for c in match.group(2).split(',')]
        body = match.group(3)
        if not body:
            continue
        for line in body.split('\n'):
            if not line:
                continue
            fields = line.split('\t')
            assert len(fields) == len(columns), (
                f"Column count mismatch on {table}: {len(fields)} vs {len(columns)}"
            )
            row = {col: unescape_pg(val) for col, val in zip(columns, fields)}
            rows[table].append(row)
    return rows


def coerce_row(row: dict, table: str) -> dict:
    """Convert raw string row → typed Python dict matching Django field expectations."""
    _, json_cols, bool_cols = TABLE_META[table]
    out: dict = {}
    for col, val in row.items():
        if val is None:
            out[col] = None
            continue
        if col in bool_cols:
            out[col] = (val == 't')
        elif col in json_cols:
            try:
                out[col] = json.loads(val) if val else None
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Failed to parse JSON in {table}.{col}: {val[:120]!r} ({exc})"
                )
        else:
            out[col] = val
    return out


def pick_target_courses(courses: list[dict]) -> list[dict]:
    """Pick a small slate: 1 math + 1 geography course.

    Prefers published school-specific courses; falls back to platform-wide
    template courses (institution_id NULL, is_published='f') when no
    published variant exists — important for math, where the production
    dump only carries the unpublished 'Mathematics S3' template. Eval
    purposes don't care about publication status — we just need
    well-formed lesson content.
    """
    def _published(rows):
        return [c for c in rows if c.get('is_published') == 't']

    math = (
        _published([c for c in courses if (c.get('subject_code') or '') == 'mathematics'])
        or _published([c for c in courses if (c.get('subject_type') or '') == 'math'])
        or [c for c in courses if (c.get('subject_type') or '') == 'math']
    )
    geo = (
        _published([c for c in courses if (c.get('subject_code') or '') == 'geography'])
        or [c for c in courses if (c.get('subject_code') or '') == 'geography']
    )

    picked: list[dict] = []
    if math:
        picked.append(math[0])
    if geo:
        picked.append(geo[0])

    if not picked:
        # Last-ditch fallback: any course at all.
        picked = courses[:2]

    return picked


def pick_target_lessons(
    lessons: list[dict],
    units_by_course: dict[str, list[dict]],
    course_ids: set[str],
    *,
    per_course_limit: int,
) -> list[dict]:
    """Return ready-state lessons under the picked courses, capped per course."""
    unit_ids_by_course: dict[str, set[str]] = {
        cid: {u['id'] for u in units_by_course.get(cid, [])}
        for cid in course_ids
    }
    chosen: list[dict] = []
    for cid, unit_ids in unit_ids_by_course.items():
        course_lessons = [
            l for l in lessons
            if l.get('unit_id') in unit_ids
            and l.get('content_status') == 'ready'
            # `is_published` filter removed — eval purposes don't care about
            # publication status; we just need well-formed lesson content
            # with ready judges + exit ticket questions.
        ]
        # Prefer lessons earlier in their unit (lower order_index → typically
        # foundational, less assumed prior knowledge).
        course_lessons.sort(
            key=lambda l: (l.get('unit_id'), int(l.get('order_index') or 0))
        )
        chosen.extend(course_lessons[:per_course_limit])
    return chosen


def row_to_fixture(table: str, row: dict) -> dict:
    """Convert one coerced row to a Django fixture entry."""
    model_label, _, _ = TABLE_META[table]
    pk = int(row['id'])
    fields: dict = {}
    for col, val in row.items():
        if col == 'id':
            continue
        # FK column rename: `course_id` → `course`, etc.
        out_col = FK_RENAMES.get(col, col)
        if out_col in NULL_OUT_FKS:
            fields[out_col] = None
            continue
        # Cast FK string values to int when present.
        if col.endswith('_id') and val is not None:
            try:
                fields[out_col] = int(val)
            except (TypeError, ValueError):
                fields[out_col] = val
        else:
            fields[out_col] = val
    return {'model': model_label, 'pk': pk, 'fields': fields}


def build_institution_fixture() -> list[dict]:
    """Return the eval-only Institution + simulator-bot User."""
    return [
        {
            'model': 'auth.user',
            'pk': EVAL_USER_PK,
            'fields': {
                'username': 'eval-bot',
                'password': '!unusable',
                'is_active': True,
                'is_staff': False,
                'is_superuser': False,
                'first_name': 'Eval',
                'last_name': 'Bot',
                'email': '',
                'date_joined': '2026-05-26T00:00:00Z',
                'last_login': None,
                'groups': [],
                'user_permissions': [],
            },
        },
        {
            'model': 'accounts.institution',
            'pk': EVAL_INSTITUTION_PK,
            'fields': {
                'name': 'Eval Harness',
                'slug': 'eval-harness',
                'timezone': 'UTC',
                'is_active': True,
                'session_mode': 'individual',
                'created_at': '2026-05-26T00:00:00Z',
                'updated_at': '2026-05-26T00:00:00Z',
            },
        },
        {
            'model': 'accounts.membership',
            'pk': EVAL_INSTITUTION_PK,
            'fields': {
                'user': EVAL_USER_PK,
                'institution': EVAL_INSTITUTION_PK,
                'role': 'student',
                'is_active': True,
                'password_reset_required': False,
                'joined_at': '2026-05-26T00:00:00Z',
            },
        },
        # ModelConfig with purpose=student_sim — required by the
        # synthetic-student driver (apps/tutoring/student_sim/) which the
        # eval harness reuses for multi_turn scenarios. Uses Anthropic
        # Haiku as default (cheap, fast, same provider as tutor/judge so
        # one API key serves all three). Override per env if needed.
        {
            'model': 'llm.modelconfig',
            'pk': EVAL_INSTITUTION_PK,
            'fields': {
                'institution': EVAL_INSTITUTION_PK,
                'name': 'Eval — Synthetic Student',
                'provider': 'anthropic',
                'model_name': 'claude-haiku-4-5-20251001',
                'api_key_env_var': 'ANTHROPIC_API_KEY',
                'api_key_encrypted': '',
                'max_tokens': 1024,
                'temperature': 0.7,
                'purpose': 'student_sim',
                'is_active': True,
                'created_at': '2026-05-26T00:00:00Z',
                'updated_at': '2026-05-26T00:00:00Z',
            },
        },
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--dump', type=Path, default=DEFAULT_DUMP,
                        help=f'Path to prod_content_dump.sql (default: {DEFAULT_DUMP})')
    parser.add_argument('--per-course-limit', type=int, default=3,
                        help='Max lessons to extract per picked course (default: 3)')
    parser.add_argument('--out-dir', type=Path, default=Path(__file__).parent,
                        help='Output directory (default: alongside this script)')
    args = parser.parse_args(argv)

    if not args.dump.exists():
        print(f"ERROR: dump not found at {args.dump}", file=sys.stderr)
        return 1

    print(f"Reading {args.dump} ...")
    sql_text = args.dump.read_text(encoding='utf-8', errors='replace')

    print("Parsing COPY blocks ...")
    raw = parse_copy_blocks(sql_text)
    for table, rows in raw.items():
        print(f"  {table}: {len(rows)} rows")

    # Stage 1: pick target courses.
    target_courses = pick_target_courses(raw['curriculum_course'])
    target_course_ids = {c['id'] for c in target_courses}
    print(f"\nPicked {len(target_courses)} target courses:")
    for c in target_courses:
        print(f"  course {c['id']}: {c['title']!r} "
              f"(subject_code={c.get('subject_code') or '-'})")

    # Stage 2: walk down: units → lessons → steps + exit tickets.
    units_in_targets = [u for u in raw['curriculum_unit']
                        if u['course_id'] in target_course_ids]
    units_by_course: dict[str, list[dict]] = {}
    for u in units_in_targets:
        units_by_course.setdefault(u['course_id'], []).append(u)

    target_lessons = pick_target_lessons(
        raw['curriculum_lesson'], units_by_course,
        target_course_ids, per_course_limit=args.per_course_limit,
    )
    target_lesson_ids = {l['id'] for l in target_lessons}
    print(f"\nPicked {len(target_lessons)} target lessons "
          f"(<= {args.per_course_limit} per course):")
    for l in target_lessons:
        print(f"  lesson {l['id']}: {l['title']!r}")

    target_steps = [s for s in raw['curriculum_lessonstep']
                    if s['lesson_id'] in target_lesson_ids]
    print(f"  → {len(target_steps)} steps")

    target_exit_tickets = [
        et for et in raw['tutoring_exitticket']
        if et.get('lesson_id') in target_lesson_ids
        or et.get('course_id') in target_course_ids
    ]
    target_et_ids = {et['id'] for et in target_exit_tickets}
    target_et_questions = [
        q for q in raw['tutoring_exitticketquestion']
        if q.get('exit_ticket_id') in target_et_ids
    ]
    print(f"  → {len(target_exit_tickets)} exit tickets "
          f"with {len(target_et_questions)} questions")

    # Stage 3: emit. Order matters: parents before children.
    fixtures: list[dict] = []

    # Reparent every extracted course to the eval institution.
    for c in target_courses:
        coerced = coerce_row(c, 'curriculum_course')
        coerced['institution_id'] = str(EVAL_INSTITUTION_PK)
        fixtures.append(row_to_fixture('curriculum_course', coerced))

    for u in units_in_targets:
        if not any(l['unit_id'] == u['id'] for l in target_lessons):
            continue  # skip units whose lessons weren't selected
        fixtures.append(row_to_fixture(
            'curriculum_unit', coerce_row(u, 'curriculum_unit'),
        ))

    for l in target_lessons:
        fixtures.append(row_to_fixture(
            'curriculum_lesson', coerce_row(l, 'curriculum_lesson'),
        ))

    for s in target_steps:
        fixtures.append(row_to_fixture(
            'curriculum_lessonstep', coerce_row(s, 'curriculum_lessonstep'),
        ))

    for et in target_exit_tickets:
        fixtures.append(row_to_fixture(
            'tutoring_exitticket', coerce_row(et, 'tutoring_exitticket'),
        ))

    for q in target_et_questions:
        fixtures.append(row_to_fixture(
            'tutoring_exitticketquestion',
            coerce_row(q, 'tutoring_exitticketquestion'),
        ))

    # Write outputs.
    inst_path = args.out_dir / 'institution.json'
    lessons_path = args.out_dir / 'lessons.json'

    inst_path.write_text(
        json.dumps(build_institution_fixture(), indent=2, ensure_ascii=False),
        encoding='utf-8',
    )
    lessons_path.write_text(
        json.dumps(fixtures, indent=2, ensure_ascii=False),
        encoding='utf-8',
    )

    print(f"\nWrote {inst_path} ({inst_path.stat().st_size} bytes)")
    print(f"Wrote {lessons_path} ({lessons_path.stat().st_size} bytes, "
          f"{len(fixtures)} fixture entries)")
    print("\nNext: python manage.py loaddata evals/fixtures/institution.json "
          "evals/fixtures/lessons.json")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
