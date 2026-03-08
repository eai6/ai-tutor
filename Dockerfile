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
COPY --from=builder /usr/local /usr/local
COPY --from=builder /root/.cache/huggingface /root/.cache/huggingface
COPY --from=builder /models/piper /models/piper
COPY . .
RUN python manage.py collectstatic --noinput
EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate && python manage.py seed_gamification && python manage.py backfill_progress && python manage.py classify_unit_grades && cp -r /app/media/vectordb /tmp/vectordb 2>/dev/null || true && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 4 --threads 4 --timeout 120"]
