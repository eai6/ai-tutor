"""Load a pg_dump --data-only SQL file into the local DB via Django ORM.

Parses each ``COPY public.<table> (cols...) FROM stdin;`` block and inserts
rows through the model layer. Django ORM handles all type coercion
(JSONField, BooleanField, DateTimeField, FK, etc.) so the dump loads
cleanly into SQLite even though it was produced by PostgreSQL.

Usage:
    python manage.py load_prod_dump prod_content_dump.sql
    python manage.py load_prod_dump prod_content_dump.sql --truncate
    python manage.py load_prod_dump prod_content_dump.sql --tables curriculum_course,curriculum_lesson

Idempotent: re-running updates existing rows by primary key. ``--truncate``
clears the target tables first (use with care).

Known dump-format quirks handled:
  - ``\\N`` -> None (SQL NULL)
  - ``t`` / ``f`` -> True / False (BooleanField)
  - Escape sequences ``\\n``, ``\\r``, ``\\t``, ``\\\\``, ``\\b``, ``\\f``, ``\\v`` in text fields
  - JSON columns parsed via json.loads
  - FK ``*_by_id`` columns that point to accounts_user are forced to None
    (the dump is curriculum-only; user rows are not included)
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


# Tables to load, in FK dependency order. (db_table, app_label.ModelName)
TABLE_MODELS: List[Tuple[str, str]] = [
    ('curriculum_course',           'curriculum.Course'),
    ('curriculum_unit',             'curriculum.Unit'),
    ('curriculum_lesson',           'curriculum.Lesson'),
    ('curriculum_lessonstep',       'curriculum.LessonStep'),
    ('tutoring_exitticket',         'tutoring.ExitTicket'),
    ('tutoring_exitticketquestion', 'tutoring.ExitTicketQuestion'),
    ('tutoring_lessonprerequisite', 'tutoring.LessonPrerequisite'),
]


# Columns that reference accounts_user — null these out since the dump
# doesn't include user rows. The fields are all nullable in the schema.
USER_FK_COLUMNS = {'teacher_approved_by_id', 'reviewed_by_id', 'created_by_id'}


# Postgres COPY escape sequences applied in this order (backslash last).
_TEXT_UNESCAPES = (
    ('\\n', '\n'),
    ('\\r', '\r'),
    ('\\t', '\t'),
    ('\\b', '\b'),
    ('\\f', '\f'),
    ('\\v', '\v'),
    ('\\\\', '\\'),
)


def _unescape_text(value: str) -> str:
    """Reverse Postgres COPY text-mode escaping."""
    for src, dst in _TEXT_UNESCAPES:
        value = value.replace(src, dst)
    return value


def _parse_pg_datetime(value: str) -> Optional[datetime]:
    """Parse Postgres timestamp ('2026-05-14 23:24:55.635847+00') to datetime."""
    if not value:
        return None
    # Normalise '+00' -> '+00:00' so fromisoformat accepts it.
    if re.search(r'[+-]\d{2}$', value):
        value = value + ':00'
    return datetime.fromisoformat(value.replace(' ', 'T'))


def _build_casters(model) -> Dict[str, Callable[[str], Any]]:
    """Return {column_name -> caster(raw_str) -> python_value} for a model.

    Uses Django field introspection so JSONField / BooleanField /
    DateTimeField / IntegerField / FK columns all get the right
    coercion automatically.
    """
    casters: Dict[str, Callable[[str], Any]] = {}

    for field in model._meta.get_fields():
        # Skip reverse relations + many-to-many descriptors
        if not getattr(field, 'concrete', False):
            continue

        column = getattr(field, 'column', None) or getattr(field, 'attname', None)
        if not column:
            continue

        internal = field.get_internal_type()

        if internal == 'JSONField':
            # Unescape Postgres COPY escapes before json.loads — otherwise
            # newlines / tabs / backslashes embedded in JSON strings come
            # through as their escape sequences and json.loads chokes.
            casters[column] = lambda v: json.loads(_unescape_text(v)) if v else None
        elif internal == 'BooleanField':
            casters[column] = lambda v: (v == 't')
        elif internal in ('IntegerField', 'BigIntegerField', 'SmallIntegerField',
                          'PositiveIntegerField', 'PositiveSmallIntegerField',
                          'AutoField', 'BigAutoField'):
            casters[column] = lambda v: int(v)
        elif internal in ('ForeignKey', 'OneToOneField'):
            casters[column] = lambda v: int(v)
        elif internal in ('FloatField', 'DecimalField'):
            casters[column] = lambda v: float(v)
        elif internal == 'DateTimeField':
            casters[column] = _parse_pg_datetime
        elif internal == 'DateField':
            casters[column] = lambda v: datetime.fromisoformat(v).date()
        else:
            # CharField, TextField, SlugField, URLField, EmailField, etc.
            casters[column] = _unescape_text

    return casters


def _iter_copy_blocks(dump_text: str) -> Iterable[Tuple[str, List[str], List[str]]]:
    """Yield (table_name, column_list, list_of_raw_data_lines) for each COPY."""
    copy_re = re.compile(r'^COPY public\.(\w+) \(([^)]+)\) FROM stdin;$', re.MULTILINE)
    for match in copy_re.finditer(dump_text):
        table = match.group(1)
        columns = [c.strip() for c in match.group(2).split(',')]
        start = match.end() + 1  # skip the trailing newline after FROM stdin;
        terminator = dump_text.find('\n\\.\n', start)
        if terminator < 0:
            continue
        body = dump_text[start:terminator]
        rows = body.split('\n') if body else []
        yield table, columns, rows


class Command(BaseCommand):
    help = "Load a pg_dump --data-only file into local DB via Django ORM."

    def add_arguments(self, parser):
        parser.add_argument('dump_path', type=str)
        parser.add_argument('--truncate', action='store_true',
                            help='Delete existing rows from target tables first')
        parser.add_argument('--tables', type=str, default='',
                            help='Comma-separated subset of tables to load')
        parser.add_argument('--dry-run', action='store_true',
                            help='Parse + validate but do not write to DB')

    def handle(self, *args, **opts):
        path = Path(opts['dump_path']).expanduser().resolve()
        if not path.exists():
            raise CommandError(f"Dump file not found: {path}")

        only = set(t.strip() for t in opts['tables'].split(',') if t.strip())
        text = path.read_text()
        blocks = list(_iter_copy_blocks(text))
        if not blocks:
            raise CommandError("No COPY blocks found in dump")

        table_to_model = dict(TABLE_MODELS)
        order_index = {t: i for i, (t, _) in enumerate(TABLE_MODELS)}

        # Sort by dependency order and filter to --tables if given
        blocks.sort(key=lambda b: order_index.get(b[0], 1_000_000))

        self.stdout.write(self.style.NOTICE(f"Loading from {path}"))

        for table, columns, rows in blocks:
            if only and table not in only:
                continue
            model_label = table_to_model.get(table)
            if not model_label:
                self.stdout.write(f"  [skip] {table} (no model mapping)")
                continue

            model = django_apps.get_model(model_label)
            casters = _build_casters(model)

            if opts['truncate'] and not opts['dry_run']:
                deleted, _ = model.objects.all().delete()
                self.stdout.write(f"  [truncate] {table}: deleted {deleted} rows")

            n_loaded = n_updated = n_skipped = 0
            with transaction.atomic():
                for line in rows:
                    if not line:
                        continue
                    fields = line.split('\t')
                    if len(fields) != len(columns):
                        n_skipped += 1
                        continue
                    obj_kwargs: Dict[str, Any] = {}
                    for col, raw in zip(columns, fields):
                        if raw == r'\N':
                            # Skip — Django uses the field default (works for
                            # NOT NULL columns that gained defaults since the
                            # dump was made, e.g. JSONField(default=dict)).
                            continue
                        if col in USER_FK_COLUMNS:
                            # Force-null: dump excludes user rows
                            obj_kwargs[col] = None
                            continue
                        caster = casters.get(col)
                        if caster is None:
                            # Column on dump but not on model — skip silently
                            continue
                        try:
                            obj_kwargs[col] = caster(raw)
                        except Exception as exc:
                            self.stderr.write(
                                f"  [warn] {table}.{col}=<{raw[:60]}>: "
                                f"{type(exc).__name__}: {exc} — skipping column"
                            )

                    pk_value = obj_kwargs.pop('id', None)
                    if opts['dry_run']:
                        n_loaded += 1
                        continue
                    if pk_value is None:
                        model.objects.create(**obj_kwargs)
                        n_loaded += 1
                    else:
                        obj, created = model.objects.update_or_create(
                            id=pk_value, defaults=obj_kwargs,
                        )
                        if created:
                            n_loaded += 1
                        else:
                            n_updated += 1

            verb = "would load" if opts['dry_run'] else "loaded"
            extra = f" updated={n_updated}" if n_updated else ""
            skipped = f" skipped={n_skipped}" if n_skipped else ""
            self.stdout.write(self.style.SUCCESS(
                f"  [{table}] {verb}={n_loaded}{extra}{skipped}"
            ))

        self.stdout.write(self.style.SUCCESS("Done."))
