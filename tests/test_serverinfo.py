"""Server health report: parsing /proc + cgroup v2 fixtures, and NL detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import serverinfo


@pytest.fixture(autouse=True)
def _decoder_ok(monkeypatch):
    """The real decode check needs faster-whisper; tests state the verdict."""
    monkeypatch.setattr(serverinfo.stt, "decode_check", lambda: (True, "ok"))


def _make_env(tmp_path: Path) -> tuple[Path, Path]:
    proc = tmp_path / "proc"
    pid1 = proc / "1"
    pid1.mkdir(parents=True)
    (proc / "meminfo").write_text("MemTotal:       2000000 kB\nMemAvailable:   1400000 kB\n")
    (proc / "loadavg").write_text("0.06 0.22 0.13 3/215 2224959\n")
    (proc / "uptime").write_text("1656971.84 3240598.31\n")
    (pid1 / "status").write_text("Name:\tpython\nVmRSS:\t  70596 kB\n")

    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "memory.max").write_text("1073741824\n")
    (cgroup / "memory.current").write_text("230600704\n")
    (cgroup / "memory.stat").write_text("anon 55209984\nfile 166006784\ninactive_file 154558464\n")
    return proc, cgroup


def test_collect_reports_all_sections(tmp_path):
    proc, cgroup = _make_env(tmp_path)
    db = tmp_path / "agent.db"
    db.write_bytes(b"x" * 2048)
    data = tmp_path / "data"
    data.mkdir()
    (data / "big.bin").write_bytes(b"y" * (1024 * 1024))

    text = serverinfo.collect(
        proc=proc, cgroup=cgroup, root=tmp_path, db_path=db, data_dir=data, stt_model="base"
    )

    assert text.startswith("🖥️ Kondisi server")
    assert "RAM bot: 68.9 MB" in text
    # working set = current - inactive_file = 76042240 bytes
    assert "RAM container: 72.5 MB / 1.0 GB (7%)" in text
    assert "RAM host: 1.3 GB bebas dari 1.9 GB (30% dipakai)" in text
    assert "CPU: " in text and "load 0.06 / 0.22 / 0.13" in text
    assert "Uptime host: 19d 4j" in text
    assert "Disk: " in text
    assert "Data: 1.0 MB" in text
    assert "Database: 2 KB" in text
    assert "Whisper: base (belum di-load, decode ok)" in text


def test_collect_skips_missing_sources(tmp_path):
    """An empty proc/cgroup must not crash; it just prints the title."""
    proc = tmp_path / "proc"
    proc.mkdir()
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    text = serverinfo.collect(proc=proc, cgroup=cgroup, root=tmp_path / "nope")
    assert text == "🖥️ Kondisi server"


def test_whisper_active_when_model_loaded(tmp_path, monkeypatch):
    proc, cgroup = _make_env(tmp_path)
    monkeypatch.setattr(serverinfo.stt, "_model", object(), raising=False)
    text = serverinfo.collect(proc=proc, cgroup=cgroup, root=tmp_path, stt_model="base")
    assert "Whisper: base (aktif di RAM, decode ok)" in text


def test_whisper_decode_failure_is_reported_not_hidden(tmp_path, monkeypatch):
    """A broken PyAV pair must show up here, not only when a voice note arrives."""
    proc, cgroup = _make_env(tmp_path)
    monkeypatch.setattr(
        serverinfo.stt,
        "decode_check",
        lambda: (False, "TypeError: unexpected keyword argument 'metadata_errors'"),
    )
    text = serverinfo.collect(proc=proc, cgroup=cgroup, root=tmp_path, stt_model="base")
    assert "DECODE GAGAL" in text
    assert "metadata_errors" in text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("cek kondisi server dong", True),
        ("cek server", True),
        ("server gimana?", True),
        ("cek ram", True),
        ("status cpu dong", True),
        ("sisa disk berapa", True),
        ("/check_server", True),
        ("berapa lama route pulang", False),
        ("ingetin aku besok jam 9 pagi", False),
        ("haha lucu banget", False),
    ],
)
def test_server_status_request(text, expected):
    assert serverinfo.server_status_request(text) is expected
