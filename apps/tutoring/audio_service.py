"""
Audio Service — configurable TTS + STT backends.

Backends:
  TTS: 'piper' (local, offline) or 'elevenlabs' (cloud, high-quality MP3)
  STT: 'whisper' (local, offline) or 'elevenlabs' (cloud, Scribe v2)

Configured via TTS_BACKEND / STT_BACKEND Django settings (default: local).
Lazy-loaded, thread-safe. Disable via DISABLE_TTS=1 / DISABLE_STT=1 env vars.
"""

import io
import logging
import os
import tempfile
import threading

from django.conf import settings

logger = logging.getLogger(__name__)

DISABLE_TTS = os.environ.get("DISABLE_TTS", "").strip() == "1"
DISABLE_STT = os.environ.get("DISABLE_STT", "").strip() == "1"


# =============================================================================
# Public API
# =============================================================================

def transcribe(audio_bytes: bytes, content_type: str = "audio/webm") -> str | None:
    """Transcribe audio bytes to text. Returns text or None on failure/disabled."""
    if DISABLE_STT:
        return None
    if settings.STT_BACKEND == 'elevenlabs':
        return _transcribe_elevenlabs(audio_bytes, content_type)
    return _transcribe_whisper(audio_bytes, content_type)


def synthesize(text: str) -> tuple[bytes | None, str]:
    """Synthesize text to audio. Returns (audio_bytes, content_type)."""
    if DISABLE_TTS:
        return None, 'audio/wav'
    if not text or not text.strip():
        return None, 'audio/wav'
    if settings.TTS_BACKEND == 'elevenlabs':
        return _synthesize_elevenlabs(text), 'audio/mpeg'
    return _synthesize_piper(text), 'audio/wav'


# =============================================================================
# STT — faster-whisper (local)
# =============================================================================

_whisper_model = None
_whisper_lock = threading.Lock()


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel
                logger.info("[STT] Loading faster-whisper tiny model...")
                _whisper_model = WhisperModel(
                    "tiny", device="cpu", compute_type="int8"
                )
                logger.info("[STT] Model loaded.")
    return _whisper_model


def _transcribe_whisper(audio_bytes: bytes, content_type: str) -> str | None:
    try:
        model = _get_whisper_model()
        suffix = ".webm" if "webm" in content_type else ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name
        try:
            segments, _info = model.transcribe(
                tmp_path, beam_size=1, language="en", vad_filter=True
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            return text if text else None
        finally:
            os.unlink(tmp_path)
    except Exception:
        logger.exception("[STT] Whisper transcription failed")
        return None


# =============================================================================
# TTS — Piper (local)
# =============================================================================

_piper_voice = None
_piper_lock = threading.Lock()

_PIPER_VOICE_NAME = os.environ.get("PIPER_VOICE", "en_US-lessac-medium")
_PIPER_MODEL_DIRS = ["/models/piper", os.path.expanduser("~/.local/share/piper_voices")]


def _download_piper_model(voice_name: str, dest_dir: str) -> str:
    """Download Piper ONNX model + JSON config from HuggingFace."""
    import urllib.request

    os.makedirs(dest_dir, exist_ok=True)
    onnx_path = os.path.join(dest_dir, f"{voice_name}.onnx")
    json_path = f"{onnx_path}.json"

    if os.path.isfile(onnx_path) and os.path.isfile(json_path):
        return onnx_path

    parts = voice_name.split("-")
    lang_country = parts[0]
    speaker = parts[1]
    quality = parts[2]
    lang = lang_country.split("_")[0]

    base_url = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
    hf_dir = f"{lang}/{lang_country}/{speaker}/{quality}"

    for filename in [f"{voice_name}.onnx", f"{voice_name}.onnx.json"]:
        dl_url = f"{base_url}/{hf_dir}/{filename}"
        dest_path = os.path.join(dest_dir, filename)
        try:
            logger.info("[TTS] Downloading %s ...", dl_url)
            urllib.request.urlretrieve(dl_url, dest_path)
        except Exception as e:
            raise RuntimeError(f"[TTS] Failed to download {filename}: {e}")

    return onnx_path


def _get_piper_voice():
    global _piper_voice
    if _piper_voice is None:
        with _piper_lock:
            if _piper_voice is None:
                from piper.voice import PiperVoice
                logger.info("[TTS] Loading Piper voice %s...", _PIPER_VOICE_NAME)

                for model_dir in _PIPER_MODEL_DIRS:
                    onnx_path = os.path.join(model_dir, f"{_PIPER_VOICE_NAME}.onnx")
                    if os.path.isfile(onnx_path):
                        _piper_voice = PiperVoice.load(onnx_path)
                        logger.info("[TTS] Loaded from %s", onnx_path)
                        return _piper_voice

                dest = _PIPER_MODEL_DIRS[-1]
                onnx_path = _download_piper_model(_PIPER_VOICE_NAME, dest)
                _piper_voice = PiperVoice.load(onnx_path)
                logger.info("[TTS] Loaded (downloaded) voice %s", _PIPER_VOICE_NAME)
    return _piper_voice


def _synthesize_piper(text: str) -> bytes | None:
    try:
        import wave
        voice = _get_piper_voice()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            voice.synthesize_wav(text, wav)
        return buf.getvalue()
    except Exception:
        logger.exception("[TTS] Piper synthesis failed")
        return None


# =============================================================================
# ElevenLabs (cloud) — shared client singleton
# =============================================================================

_elevenlabs_client = None
_elevenlabs_lock = threading.Lock()


def _get_elevenlabs_client():
    global _elevenlabs_client
    if _elevenlabs_client is None:
        with _elevenlabs_lock:
            if _elevenlabs_client is None:
                from elevenlabs import ElevenLabs
                api_key = settings.ELEVENLABS_API_KEY
                if not api_key:
                    raise RuntimeError("ELEVENLABS_API_KEY not configured")
                _elevenlabs_client = ElevenLabs(api_key=api_key)
                logger.info("[ElevenLabs] Client initialized")
    return _elevenlabs_client


def _transcribe_elevenlabs(audio_bytes: bytes, content_type: str) -> str | None:
    try:
        client = _get_elevenlabs_client()
        suffix = ".webm" if "webm" in content_type else ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name
        try:
            with open(tmp_path, "rb") as audio_file:
                result = client.speech_to_text.convert(
                    file=audio_file,
                    model_id="scribe_v2",
                    language_code="en",
                )
            return result.text if result.text else None
        finally:
            os.unlink(tmp_path)
    except Exception:
        logger.exception("[STT] ElevenLabs transcription failed")
        return None


def _synthesize_elevenlabs(text: str) -> bytes | None:
    try:
        client = _get_elevenlabs_client()
        audio_gen = client.text_to_speech.convert(
            voice_id=settings.ELEVENLABS_VOICE_ID,
            text=text,
            model_id=settings.ELEVENLABS_MODEL_ID,
            output_format="mp3_44100_128",
        )
        # Collect generator chunks into bytes
        chunks = []
        for chunk in audio_gen:
            chunks.append(chunk)
        return b"".join(chunks)
    except Exception:
        logger.exception("[TTS] ElevenLabs synthesis failed")
        return None
