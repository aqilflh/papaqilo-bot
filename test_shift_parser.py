"""Shift parser: every example from the spec plus date/rollover edge cases.

The parser must be deterministic: same text in, same rows out. These tests pin
the calendar facts to 2026 (the fixture month), so a weekday mismatch has a
real date to disagree with.
"""

from __future__ import annotations

from datetime import date

from app.shift_parser import (
    count_matching_lines,
    parse_schedule,
    resolve_manual_date,
)

TODAY = date(2026, 9, 28)  # a Monday


def _parse(text: str):
    return parse_schedule(text, TODAY)


# ------------------------------------------------------------------ detection


def test_count_matching_lines_counts_only_real_entries():
    text = "JUDUL\n\nOKTOBER\n1 kamis igd malam\n2 jumat klinik siang\n3 sabtu libur\n4 minggu rs\n5 senin xyz"
    assert count_matching_lines(text) == 5


def test_count_matching_lines_zero_for_chat():
    assert count_matching_lines("cuaca hari ini gimana") == 0


# ---------------------------------------------------------------- month header


def test_month_header_sets_base_and_parses_kind_place_partner():
    parsed = _parse("OKTOBER\n1 kamis igd malam (+kang dodi)")
    assert parsed.ambiguous is False
    assert parsed.base == (2026, 10)
    entry = parsed.savable[0]
    assert entry.date == date(2026, 10, 1)
    assert entry.weekday_ok is True
    assert entry.kind == "malam"
    assert entry.place == "igd"
    assert entry.partners == ["Kang Dodi"]


def test_libur_is_a_kind():
    entry = _parse("OKTOBER\n10 sabtu libur").entries[0]
    assert entry.kind == "libur"
    assert entry.date == date(2026, 10, 10)
    assert entry.is_savable


def test_malem_means_malam():
    entry = _parse("OKTOBER\n8 kamis malem rs (+cici)").entries[0]
    assert entry.kind == "malam"
    assert entry.place == "rs"
    assert entry.partners == ["Cici"]


def test_pagi_slash_note_becomes_note():
    entry = _parse("OKTOBER\n15 kamis igd pagi/prolanis").entries[0]
    assert entry.kind == "pagi"
    assert entry.place == "igd"
    assert entry.note == "prolanis"


def test_parenthesized_note_plus_partner():
    entry = _parse("OKTOBER\n28 rabu rs pagi (ranap) (+ani)").entries[0]
    assert entry.kind == "pagi"
    assert entry.place == "rs"
    assert entry.note == "ranap"
    assert entry.partners == ["Ani"]


def test_no_colon_and_space_after_plus():
    parsed = _parse("OKTOBER\n13 selasa klinik siang (+ budi)")
    entry = parsed.entries[0]
    assert entry.kind == "siang"
    assert entry.place == "klinik"
    assert entry.partners == ["Budi"]
    assert entry.weekday_ok is True


def test_multiple_partners_split_on_ampersand_and_dan():
    entry = _parse("OKTOBER\n6 selasa klinik siang (+ani & budi)").entries[0]
    assert entry.partners == ["Ani", "Budi"]
    entry = _parse("OKTOBER\n6 selasa klinik siang (+ani dan budi)").entries[0]
    assert entry.partners == ["Ani", "Budi"]


def test_ahad_and_jumat_spellings():
    parsed = _parse("OKTOBER\n4 ahad rs pagi\n9 jum'at klinik siang")
    assert parsed.entries[0].kind == "pagi"
    assert parsed.entries[0].weekday_ok is True  # Oct 4 2026 is Sunday
    assert parsed.entries[1].kind == "siang"
    assert parsed.entries[1].weekday_ok is True  # Oct 9 2026 is Friday


# ---------------------------------------------------------------- empty lines


def test_empty_remainder_is_belum_ada_jadwal_not_libur():
    entry = _parse("OKTOBER\n24 sabtu").entries[0]
    assert entry.status == "empty"
    assert not entry.is_savable
    assert any("belum ada jadwal" in w for w in entry.warnings)


def test_title_and_junk_lines_are_ignored():
    parsed = _parse("JADWAL CONTOH DI KOTA X\napa ini?\nOKTOBER\n1 kamis igd malam")
    assert any(line.startswith("JADWAL") for line in parsed.ignored)
    assert any("apa ini?" in line for line in parsed.ignored)
    assert len(parsed.savable) == 1


# ------------------------------------------------------------------ rollover


def test_month_rollover_without_header():
    # Two entries: Oct 28 is rabu, and "2 senin" after a day 28 means Nov 2.
    parsed = _parse("28 rabu igd\n2 senin klinik siang")
    assert (parsed.ambiguous, parsed.base) == (False, (2026, 10))
    assert parsed.entries[0].date == date(2026, 10, 28)
    assert parsed.entries[1].date == date(2026, 11, 2)
    assert parsed.entries[1].weekday_ok is True


def test_first_block_without_header_picks_current_month():
    parsed = _parse("28 senin klinik siang")
    assert parsed.base == (2026, 9)
    assert parsed.entries[0].date == date(2026, 9, 28)


# ------------------------------------------------------------- warning flags


def test_weekday_mismatch_flagged_but_kept():
    entry = _parse("OKTOBER\n28 senin klinik siang").entries[0]
    assert entry.date == date(2026, 10, 28)  # really a rabu
    assert entry.weekday_ok is False
    assert entry.is_savable, "a weekday mismatch must not drop the row"
    assert any("bukan hari senin" in w for w in entry.warnings)


def test_ambiguous_pagi_siang_flagged():
    entry = _parse("OKTOBER\n15 kamis igd pagi/siang").entries[0]
    assert entry.kind_ambiguous is True
    assert not entry.is_savable
    assert any("ambigu" in w for w in entry.warnings)


def test_unrecognizable_kind_is_flagged():
    entry = _parse("OKTOBER\n15 kamis igd aneh").entries[0]
    assert entry.kind is None
    assert not entry.is_savable
    assert any("tidak bisa dikenali" in w for w in entry.warnings)


def test_invalid_date_is_ignored_not_crashed():
    entry = _parse("APRIL\n31 jumat klinik siang").entries[0]
    assert entry.date is None
    assert entry.status == "ignored"


# ------------------------------------------------------------- ambiguity table


def test_first_block_previous_month_inference():
    # On 28 Sep 2026 (Monday), "10 senin" uniquely matches Aug 10 within the
    # {prev, current, next} window, so August is the base, not an error.
    parsed = _parse("10 senin")
    assert parsed.ambiguous is False
    assert parsed.base == (2026, 8)
    assert parsed.entries[0].date == date(2026, 8, 10)


def test_zero_matching_candidates_means_ambiguous():
    # "15 senin" matches none of the {Aug, Sep, Oct 2026} candidates -> ask.
    parsed = _parse("15 senin")
    assert parsed.ambiguous is True
    assert parsed.base is None


# ------------------------------------------------------------ manual /jaga date


def test_resolve_manual_date_future_this_year():
    assert resolve_manual_date(TODAY, 22, 10) == date(2026, 10, 22)


def test_resolve_manual_date_past_goes_next_year():
    assert resolve_manual_date(TODAY, 5, 3) == date(2027, 3, 5)


def test_resolve_manual_date_invalid_returns_none():
    assert resolve_manual_date(TODAY, 31, 2) is None
