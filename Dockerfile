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

# Export the MiniLM encoder to ONNX.
#
# The runtime uses onnxruntime + tokenizers, both already dependencies, and
# needs no torch to embed. Doing the conversion HERE — in the builder, from the
# sentence-transformers weights cached in the layer above — means the graph
# provably corresponds to the encoder it replaces, and the ~90 MB artifact is
# the only thing carried forward.
#
# --skip-parity because the comparison against sentence-transformers goes
# through kb_storage, which needs a configured Django — the builder has the
# dependencies but not the app source or its settings. That comparison runs in
# CI instead (test_onnx_embedding_parity.py), against this same artifact.
#
# It is built rather than copied because models/ is a gitignored build
# artifact: a CI checkout has no models/ directory, so `COPY . .` would ship an
# image with no encoder. Embedding would then fail into a caught exception,
# retrieval would return nothing, and the tutor would answer ungrounded with
# only a log line to say so.
RUN pip install --no-cache-dir "onnx>=1.22,<2"
COPY scripts/export_minilm_onnx.py /build/scripts/
RUN python /build/scripts/export_minilm_onnx.py --skip-parity \
 && test -s /build/models/minilm-l6-v2/model.onnx \
 && test -s /build/models/minilm-l6-v2/tokenizer.json

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
# After `COPY . .` deliberately: the build context may carry a local models/
# directory, and the artifact built above is the one that must win.
COPY --from=builder /build/models/minilm-l6-v2 /app/models/minilm-l6-v2
RUN python manage.py collectstatic --noinput
EXPOSE 8000
# Vectors now live in Postgres via pgvector — no more /tmp/vectordb
# SMB-SQLite workaround copy. See memory/pgvector_migration_plan.md.
CMD ["sh", "-c", "python manage.py migrate && python manage.py seed_gamification && python manage.py backfill_progress && python manage.py classify_unit_grades && python manage.py seed_help_assistant_model && python manage.py generate_recent_updates && python manage.py build_help_index --with-source && gunicorn ai_tutor.config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 8 --timeout 120"]
