"""Speech-to-text with a locally hosted Whisper model.

faster-whisper (CTranslate2) runs on the CPU with int8, so the base model fits
the small VPS and no audio ever leaves the server. Both the ``faster_whisper``
import and the weights are lazy: importing it pulls in ctranslate2 and av, and
the model costs a few hundred megabytes of RAM, so neither happens until a voice
note actually arrives.

The model is a process-wide singleton guarded by a lock: two voice notes at the
same time must not each allocate a copy of the weights, and on a 2-vCPU box they
must not run at the same time either.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import wave
from contextlib import suppress
from pathlib import Path

#: "base" is the sweet spot for short Indonesian clips on 2 vCPUs. "small" is
#: noticeably better but needs ~600 MB and roughly double the time.
DEFAULT_MODEL = "base"
DEFAULT_COMPUTE_TYPE = "int8"
DEFAULT_CPU_THREADS = 2
DEFAULT_LANGUAGE = "id"

#: Serializes both model loading and transcription (single-flight).
_lock = threading.Lock()
_model = None
_model_key: tuple[str, str, int] | None = None


class SttError(RuntimeError):
    """Raised when the model cannot be loaded or the audio cannot be decoded."""


def _build_model(model_name: str, compute_type: str, cpu_threads: int):
    # Imported here, not at module top: the import alone is heavy and tests
    # must be able to import this module without faster-whisper installed.
    from faster_whisper import WhisperModel

    return WhisperModel(
        model_name,
        device="cpu",
        compute_type=compute_type,
        cpu_threads=cpu_threads,
    )


def _get_model(model_name: str, compute_type: str, cpu_threads: int):
    """Return the cached model, building it on first use or after a key change."""
    global _model, _model_key
    key = (model_name, compute_type, cpu_threads)
    if _model is not None and _model_key == key:
        return _model
    _model = _build_model(model_name, compute_type, cpu_threads)
    _model_key = key
    return _model


def reset() -> None:
    """Drop the cached model. For tests only."""
    global _model, _model_key
    _model = None
    _model_key = None


def transcribe_sync(
    path: str | Path,
    *,
    model_name: str = DEFAULT_MODEL,
    compute_type: str = DEFAULT_COMPUTE_TYPE,
    cpu_threads: int = DEFAULT_CPU_THREADS,
    language: str = DEFAULT_LANGUAGE,
) -> str:
    """Blocking transcription of one audio file. Returns the joined transcript.

    Kept synchronous so the runner can push it onto a worker thread and so
    tests can call it without an event loop. Any failure surfaces as SttError.
    """
    try:
        with _lock:
            model = _get_model(model_name, compute_type, cpu_threads)
            segments, _info = model.transcribe(
                str(path),
                language=language or None,
                beam_size=1,
                vad_filter=True,
            )
            return " ".join(segment.text.strip() for segment in segments).strip()
    except SttError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize decode/model/disk errors
        raise SttError(str(exc)) from exc


async def transcribe(
    path: str | Path,
    *,
    model_name: str = DEFAULT_MODEL,
    compute_type: str = DEFAULT_COMPUTE_TYPE,
    cpu_threads: int = DEFAULT_CPU_THREADS,
    language: str = DEFAULT_LANGUAGE,
) -> str:
    """Async wrapper: the CPU-bound decode runs in a worker thread.

    Must not run on the event loop, or polling would stall for the whole clip.
    """
    return await asyncio.to_thread(
        transcribe_sync,
        path,
        model_name=model_name,
        compute_type=compute_type,
        cpu_threads=cpu_threads,
        language=language,
    )


def decode_check() -> tuple[bool, str]:
    """Can this install decode audio at all? Returns (ok, detail).

    faster-whisper hands the file straight to PyAV instead of shelling out to
    ffmpeg, so the two libraries have to agree. They did not: faster-whisper
    1.2.1 calls ``av.open(..., metadata_errors="ignore")`` and PyAV 16 removed
    that argument, which broke every voice note at decode time, before the
    model ever ran, and only became visible when a user sent one.

    Decoding a tiny generated WAV costs milliseconds and never loads the model,
    so /check_server can report the breakage instead of hiding it behind
    "belum di-load".
    """
    path: Path | None = None
    try:
        from faster_whisper.audio import decode_audio

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            path = Path(handle.name)
        with wave.open(str(path), "w") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(b"\x00\x00" * 1600)
        samples = decode_audio(str(path), sampling_rate=16000)
        return (True, "ok") if len(samples) else (False, "audio kosong")
    except ImportError:
        return False, "faster-whisper tidak terpasang"
    except Exception as exc:  # noqa: BLE001 - the message is the whole point
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if path is not None:
            with suppress(OSError):
                path.unlink()
