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
import threading
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
