# Stage 1: Build dependencies
FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
# Install all deps into system Python — CPU-only torch first, then everything else
RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download Whisper tiny model for STT
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"

# Pre-download Piper voice model for TTS (ONNX + JSON config)
RUN mkdir -p /models/piper && \
    python -c "import urllib.request; \
    base='https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium'; \
    urllib.request.urlretrieve(f'{base}/en_US-lessac-medium.onnx', '/models/piper/en_US-lessac-medium.onnx'); \
    urllib.request.urlretrieve(f'{base}/en_US-lessac-medium.onnx.json', '/models/piper/en_US-lessac-medium.onnx.json')"

# Stage 2: Runtime
FROM python:3.12-slim
WORKDIR /app

# psql / pg_dump / pg_restore. psycopg2-binary is a driver and ships no
# executables, so without this there is no way to run a restore from inside the
# VPC — and RDS is deliberately not publicly accessible, so from inside the VPC
# is the only safe place to do it.
#
# Must be in the RUNTIME stage. Only /usr/local, the HF cache and /models/piper
# are copied forward from the builder, and Debian puts these in /usr/bin.
#
# Pinned to 16 to match the RDS engine. pg_restore cannot read an archive
# produced by a NEWER pg_dump than itself, and our dumps come off a Postgres 16
# server — so whatever the base image happens to ship is not good enough.
# Hence the PGDG repo.
#
# The suite is read from /etc/os-release rather than hardcoded. The first
# attempt said "bookworm" and the build failed resolving libpq5, because
# python:3.12-slim has moved to trixie — apt was being handed packages built
# for a different Debian release. Deriving it means the next base-image bump
# does not break this again.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && . /etc/os-release \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc]\
 https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-16 \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local /usr/local
COPY --from=builder /root/.cache/huggingface /root/.cache/huggingface
COPY --from=builder /models/piper /models/piper
COPY . .
RUN python manage.py collectstatic --noinput
EXPOSE 8000
# Vectors now live in Postgres via pgvector — no more /tmp/vectordb
# SMB-SQLite workaround copy. See memory/pgvector_migration_plan.md.
CMD ["sh", "-c", "python manage.py migrate && python manage.py seed_gamification && python manage.py backfill_progress && python manage.py classify_unit_grades && python manage.py seed_help_assistant_model && python manage.py generate_recent_updates && python manage.py build_help_index --with-source && gunicorn ai_tutor.config.wsgi:application --bind 0.0.0.0:8000 --workers 4 --threads 4 --timeout 120"]
