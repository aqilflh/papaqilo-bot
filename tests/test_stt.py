"""Local speech-to-text: lazy loading, transcript joining, error wrapping.

faster-whisper is replaced with a fake module so these tests run without the
real dependency, its weights, or a GPU.
"""

from __future__ import annotations

import sys
import types

import pytest

from app import stt


class _FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModel:
    def __init__(self, model_name: str, **kwargs: object) -> None:
        self.model_name = model_name
        self.kwargs = kwargs
        self.transcribe_calls: list[tuple[str, dict]] = []

    def transcribe(self, path: str, **kwargs: object):
        self.transcribe_calls.append((path, kwargs))
        return [_FakeSegment(" halo "), _FakeSegment("dunia")], {"language": "id"}


@pytest.fixture(autouse=True)
def fake_whisper(monkeypatch):
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = _FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    stt.reset()
    yield
    stt.reset()


def _audio(tmp_path):
    path = tmp_path / "note.oga"
    path.write_bytes(b"fake-opus")
    return path


def test_transcribe_joins_segments(tmp_path):
    assert stt.transcribe_sync(_audio(tmp_path)) == "halo dunia"


def test_transcribe_uses_cpu_int8_and_forces_beam_one(tmp_path):
    stt.transcribe_sync(_audio(tmp_path), model_name="base", language="id")
    model = stt._model
    assert model.model_name == "base"
    assert model.kwargs["device"] == "cpu"
    assert model.kwargs["compute_type"] == "int8"
    _path, kwargs = model.transcribe_calls[0]
    assert kwargs["language"] == "id"
    assert kwargs["beam_size"] == 1
    assert kwargs["vad_filter"] is True


def test_empty_language_becomes_none_for_auto_detect(tmp_path):
    stt.transcribe_sync(_audio(tmp_path), language="")
    _path, kwargs = stt._model.transcribe_calls[0]
    assert kwargs["language"] is None


def test_model_is_cached_between_calls(tmp_path):
    stt.transcribe_sync(_audio(tmp_path))
    first = stt._model
    stt.transcribe_sync(_audio(tmp_path))
    assert stt._model is first


def test_model_rebuilds_when_name_changes(tmp_path):
    stt.transcribe_sync(_audio(tmp_path), model_name="base")
    first = stt._model
    stt.transcribe_sync(_audio(tmp_path), model_name="small")
    assert stt._model is not first


def test_load_errors_are_wrapped_as_stt_error(tmp_path, monkeypatch):
    class _Boom:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise ValueError("no model for you")

    monkeypatch.setattr(sys.modules["faster_whisper"], "WhisperModel", _Boom)
    with pytest.raises(stt.SttError):
        stt.transcribe_sync(_audio(tmp_path))


def test_decode_errors_are_wrapped_as_stt_error(tmp_path):
    class _Boom(_FakeModel):
        def transcribe(self, path: str, **kwargs: object):
            raise RuntimeError("bad audio")

    stt.reset()
    sys.modules["faster_whisper"].WhisperModel = _Boom
    with pytest.raises(stt.SttError):
        stt.transcribe_sync(_audio(tmp_path))


async def test_async_wrapper_transcribes_off_the_loop(tmp_path):
    assert await stt.transcribe(_audio(tmp_path)) == "halo dunia"


# --------------------------------------------------------------- decode check


def test_decode_check_reports_a_missing_decoder(monkeypatch):
    """Local dev has no faster-whisper; that must read as a warning, not a crash."""
    monkeypatch.delitem(sys.modules, "faster_whisper", raising=False)
    ok, detail = stt.decode_check()
    assert ok is False
    assert "tidak terpasang" in detail


def test_decode_check_reports_a_pyav_signature_mismatch(monkeypatch):
    """The exact production bug: faster-whisper's call no longer fits PyAV."""
    import types as _types

    module = _types.ModuleType("faster_whisper.audio")

    def decode_audio(path, sampling_rate=16000):
        raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")

    module.decode_audio = decode_audio
    package = sys.modules["faster_whisper"]
    monkeypatch.setattr(package, "audio", module, raising=False)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", module)

    ok, detail = stt.decode_check()
    assert ok is False
    assert "metadata_errors" in detail


def test_decode_check_passes_with_a_working_decoder(monkeypatch):
    import types as _types

    module = _types.ModuleType("faster_whisper.audio")
    module.decode_audio = lambda path, sampling_rate=16000: [0.0] * 100
    package = sys.modules["faster_whisper"]
    monkeypatch.setattr(package, "audio", module, raising=False)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", module)

    assert stt.decode_check() == (True, "ok")


def test_decode_check_reports_a_silent_decode(monkeypatch):
    import types as _types

    module = _types.ModuleType("faster_whisper.audio")
    module.decode_audio = lambda path, sampling_rate=16000: []
    package = sys.modules["faster_whisper"]
    monkeypatch.setattr(package, "audio", module, raising=False)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", module)

    ok, detail = stt.decode_check()
    assert ok is False
    assert "kosong" in detail
