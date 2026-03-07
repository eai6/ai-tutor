"""
Audio Service — Piper TTS + faster-whisper STT singletons.

Lazy-loaded, thread-safe. Disable via DISABLE_TTS=1 / DISABLE_STT=1 env vars.
"""

import io
import logging
import os
import tempfile
import threading

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# STT — faster-whisper
# ---------------------------------------------------------------------------

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


def transcribe(audio_bytes: bytes, content_type: str = "audio/webm") -> str | None:
    """Transcribe audio bytes to text using faster-whisper.

    Returns transcribed text or None on failure / disabled.
    """
    if os.environ.get("DISABLE_STT", "").strip() == "1":
        return None

    try:
        model = _get_whisper_model()

        # Write to temp file (faster-whisper needs a file path)
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
        logger.exception("[STT] Transcription failed")
        return None


# ---------------------------------------------------------------------------
# TTS — Piper
# ---------------------------------------------------------------------------

_piper_voice = None
_piper_lock = threading.Lock()

_PIPER_VOICE_NAME = os.environ.get("PIPER_VOICE", "en_US-lessac-medium")
_PIPER_MODEL_DIRS = ["/models/piper", os.path.expanduser("~/.local/share/piper_voices")]


def _download_piper_model(voice_name: str, dest_dir: str) -> str:
    """Download Piper ONNX model + JSON config from HuggingFace.

    Voice name format: ``en_US-lessac-medium``
      → HF path: ``en/en_US/lessac/medium/en_US-lessac-medium.onnx``

    Returns path to the .onnx file.
    """
    import urllib.request

    os.makedirs(dest_dir, exist_ok=True)
    onnx_path = os.path.join(dest_dir, f"{voice_name}.onnx")
    json_path = f"{onnx_path}.json"

    if os.path.isfile(onnx_path) and os.path.isfile(json_path):
        return onnx_path

    # Parse voice name: en_US-lessac-medium → lang=en, country=en_US, speaker=lessac, quality=medium
    parts = voice_name.split("-")  # ["en_US", "lessac", "medium"]
    lang_country = parts[0]        # "en_US"
    speaker = parts[1]             # "lessac"
    quality = parts[2]             # "medium"
    lang = lang_country.split("_")[0]  # "en"

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

                # Try each model directory for existing files
                for model_dir in _PIPER_MODEL_DIRS:
                    onnx_path = os.path.join(model_dir, f"{_PIPER_VOICE_NAME}.onnx")
                    if os.path.isfile(onnx_path):
                        _piper_voice = PiperVoice.load(onnx_path)
                        logger.info("[TTS] Loaded from %s", onnx_path)
                        return _piper_voice

                # Download model from HuggingFace
                dest = _PIPER_MODEL_DIRS[-1]
                onnx_path = _download_piper_model(_PIPER_VOICE_NAME, dest)
                _piper_voice = PiperVoice.load(onnx_path)
                logger.info("[TTS] Loaded (downloaded) voice %s", _PIPER_VOICE_NAME)
    return _piper_voice


def synthesize(text: str) -> bytes | None:
    """Synthesize text to WAV bytes using Piper TTS.

    Returns WAV bytes or None on failure / disabled.
    """
    if os.environ.get("DISABLE_TTS", "").strip() == "1":
        return None

    if not text or not text.strip():
        return None

    try:
        import wave
        voice = _get_piper_voice()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            voice.synthesize_wav(text, wav)
        return buf.getvalue()
    except Exception:
        logger.exception("[TTS] Synthesis failed")
        return None
