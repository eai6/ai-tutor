"""SQLite-tolerant ``VectorField`` wrapper.

The production database is Azure Postgres with the ``vector`` extension
installed. Local development + CI eval both use SQLite, which has no
``vector`` column type. To keep Django migrations runnable on every
backend, this wrapper subclasses ``pgvector.django.VectorField`` and
falls back to a generic text column on non-Postgres backends.

Vector similarity queries (``CosineDistance`` etc.) will not work on
SQLite — callers are expected to gate by ``connection.vendor`` if they
need to support both. In practice, the KB layer simply returns empty
results on SQLite because no real vectors are stored there.

Usage in models:

    from ai_tutor.apps.curriculum.vector_field import VectorField

    class CurriculumChunk(models.Model):
        embedding = VectorField(dimensions=384)
"""
from __future__ import annotations

from pgvector.django import VectorField as _PgVectorField


class VectorField(_PgVectorField):
    """``pgvector`` VectorField with a SQLite/MySQL fallback type.

    On Postgres the column is ``vector(N)`` exactly as upstream
    pgvector-django defines it. On any non-Postgres backend the column
    is created as ``TEXT`` so migrations succeed cleanly; the value is
    serialised by Django as the field's string representation.
    """

    def db_type(self, connection):
        if connection.vendor == 'postgresql':
            return super().db_type(connection)
        return 'text'
