"""Message splitting, because Telegram silently drops a message over 4096 chars."""

from __future__ import annotations

from app.bot import CHUNK_SIZE, TELEGRAM_LIMIT, split_message


def test_short_text_is_one_chunk():
    assert split_message("halo") == ["halo"]


def test_empty_text_is_still_one_chunk():
    assert split_message("") == [""]


def test_text_at_the_limit_is_one_chunk():
    text = "a" * CHUNK_SIZE
    assert split_message(text) == [text]


def test_every_chunk_fits_the_telegram_limit():
    text = "kalimat yang cukup panjang. " * 900
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= CHUNK_SIZE for c in chunks)
    assert all(len(c) <= TELEGRAM_LIMIT for c in chunks)


def test_split_preserves_all_content():
    # No characters dropped and none duplicated, ignoring all whitespace.
    # Whitespace is what rstrip/lstrip on the cut points throws away.
    text = "\n\n".join(f"Paragraf nomor {i} dengan isi yang somewhat panjang." for i in range(300))
    joined = "".join(split_message(text))
    squash = lambda s: "".join(s.split())  # noqa: E731
    assert squash(joined) == squash(text)


def test_split_prefers_paragraph_boundaries():
    # Each paragraph is its own block, so a good split breaks between them
    # rather than mid-paragraph.
    para = "Kata " * 200  # ~1000 chars, no internal newlines
    text = "\n\n".join([para] * 8)
    chunks = split_message(text)

    assert len(chunks) > 1
    for chunk in chunks:
        # A chunk is a run of whole paragraphs: it must not begin or end on a
        # newline, and it must not end mid-word. Newlines *between* the
        # paragraphs it carries are expected and preserved.
        assert not chunk.startswith("\n")
        assert not chunk.endswith("\n")
        assert chunk.startswith("Kata")
        assert chunk.strip().endswith("Kata")
        # Every word in every chunk is complete.
        assert all(word == "Kata" for word in chunk.split())

    # No paragraph was split across a chunk boundary: all 8 paragraphs' worth
    # of words survive, in complete words, and 3 paragraphs fit per chunk.
    assert sum(c.count("Kata") for c in chunks) == 8 * 200
    assert len(chunks) == 3
    # The two separators that fell on cut points are stripped; the five
    # separators inside chunks are preserved.
    assert "".join(chunks).count("\n\n") == 5


def test_unbreakable_text_is_hard_cut_not_infinite_loop():
    text = "y" * (CHUNK_SIZE * 2 + 500)
    chunks = split_message(text)
    assert len(chunks) == 3
    assert all(len(c) <= CHUNK_SIZE for c in chunks)
    assert "".join(chunks) == text


def test_custom_limit_is_respected():
    text = "z" * 250
    chunks = split_message(text, limit=100)
    assert len(chunks) == 3
    assert all(len(c) <= 100 for c in chunks)
