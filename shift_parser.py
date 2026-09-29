"""Parse an Indonesian shift schedule into rows, deterministically, no LLM.

The pasted text is untrusted input and is never sent to the LLM (SPEC Phase 9:
"Never send the schedule text to the LLM"). This module only pattern-matches
and computes dates. If the month cannot be decided from the weekdays it says so
instead of guessing, because a wrong month would silently shift every shift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

#: Written weekday name -> Python's Monday-indexed date.weekday().
DAYS = {
    "senin": 0,
    "selasa": 1,
    "rabu": 2,
    "kamis": 3,
    "jumat": 4,
    "jum'at": 4,
    "sabtu": 5,
    "minggu": 6,
    "ahad": 6,
}

MONTHS = {
    "januari": 1,
    "februari": 2,
    "maret": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "agustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "desember": 12,
}

#: "malem" is colloquial Indonesian for "malam"; both normalize to one value.
TIME_WORDS = {"pagi": "pagi", "siang": "siang", "malam": "malam", "malem": "malam"}

#: First known token becomes `place`. Extend the list, not the parser.
PLACES = ("igd", "klinik", "rs")

# The line shape from the spec, verbatim (case-insensitive).
LINE_RE = re.compile(
    r"^\s*(\d{1,2})\s+(senin|selasa|rabu|kamis|jumat|jum'at|sabtu|minggu|ahad)\s*:?\s*(.*)$",
    re.IGNORECASE,
)
MONTH_RE = re.compile(r"^\s*([a-z]+)\s*$", re.IGNORECASE)
GROUP_RE = re.compile(r"\(([^()]*)\)")
WORD_RE = re.compile(r"[a-z']+", re.IGNORECASE)


@dataclass
class ParsedLine:
    day: int
    date: date | None  # None when the (year, month, day) combination is invalid
    written_day: str  # weekday exactly as written, normalized lowercase
    remainder: str  # what came after the weekday, before any cleanup
    raw: str  # the original pasted line
    weekday_ok: bool = False  # written weekday matches the computed date
    kind: str | None = None  # 'pagi' | 'siang' | 'malam' | 'libur'
    kind_ambiguous: bool = False  # e.g. "pagi/siang": two time-of-day words
    place: str | None = None
    note: str | None = None
    partners: list[str] = field(default_factory=list)
    status: str = "shift"  # 'shift' | 'empty' (belum ada jadwal) | 'ignored'
    warnings: list[str] = field(default_factory=list)

    @property
    def is_savable(self) -> bool:
        """A row can be stored only when it has a single, unambiguous kind."""
        return (
            self.status == "shift"
            and self.date is not None
            and self.kind is not None
            and not self.kind_ambiguous
        )


@dataclass
class ScheduleParse:
    entries: list[ParsedLine]  # every record that matched the line pattern
    ignored: list[str]  # non-matching lines (title, junk), reported in the preview
    base: tuple[int, int] | None  # (year, month) chosen for the first block
    ambiguous: bool = False  # month could not be decided -> ask, don't guess

    @property
    def savable(self) -> list[ParsedLine]:
        return [entry for entry in self.entries if entry.is_savable]


def count_matching_lines(text: str) -> int:
    """How many lines look like shift entries. Used to auto-detect an import."""
    return sum(1 for line in text.splitlines() if LINE_RE.match(line.strip()))


def parse_schedule(text: str, today: date) -> ScheduleParse:
    """Parse pasted schedule text against `today` (the user's local date).

    Returns entries plus the ignored lines. `ambiguous` is True when the first
    block's month cannot be decided; the caller must ask the user instead of
    saving anything.
    """
    tokens, ignored = _tokenize(text)
    records = [token for token in tokens if token[0] == "r"]
    if not records:
        return ScheduleParse(entries=[], ignored=ignored, base=None, ambiguous=True)

    base = _pick_base(tokens, today)
    if base is None:
        return ScheduleParse(entries=[], ignored=ignored, base=None, ambiguous=True)

    entries, _ = _resolve(tokens, base[0], base[1])
    return ScheduleParse(entries=entries, ignored=ignored, base=base, ambiguous=False)


def resolve_manual_date(today: date, day: int, month: int) -> date | None:
    """Resolve a /jaga dd/mm date: this year if still today-or-future, else next.

    Manual entries are always forward-looking, so a past date means the user
    meant next year rather than a typo'd date we silently store in the past.
    """
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        return None
    if candidate < today:
        try:
            candidate = date(today.year + 1, month, day)
        except ValueError:
            return None
    return candidate


# ---------------------------------------------------------------------------
# tokenization
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> tuple[list[tuple], list[str]]:
    """Split the input into ordered tokens.

    Each token is either ``("h", month_number, raw)`` for a month header or
    ``("r", day, weekday_name, remainder, raw)`` for a shift line. Non-matching
    lines go to the `ignored` bucket.
    """
    tokens: list[tuple] = []
    ignored: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = MONTH_RE.match(stripped)
        if match and match.group(1).lower() in MONTHS:
            tokens.append(("h", MONTHS[match.group(1).lower()], stripped))
            continue
        match = LINE_RE.match(stripped)
        if match is None:
            ignored.append(stripped)
            continue
        day = int(match.group(1))
        weekday = match.group(2).lower()
        remainder = match.group(3).strip()
        tokens.append(("r", day, weekday, remainder, stripped))
    return tokens, ignored


# ---------------------------------------------------------------------------
# month inference
# ---------------------------------------------------------------------------


def _pick_base(tokens: list[tuple], today: date) -> tuple[int, int] | None:
    """Decide the (year, month) of the first block.

    A leading month header decides for us. Otherwise score the three candidate
    months {previous, current, next} relative to today by how many written
    weekdays match the real calendar; pick the uniquely best one. Zero matches
    or a tie is ambiguous -> ask, never guess.
    """
    if tokens[0][0] == "h":
        month = int(tokens[0][1])
        year = today.year
        if month < today.month:
            year += 1
        return (year, month)

    scores: list[tuple[int, tuple[int, int]]] = []
    for year, month in _month_candidates(today):
        entries, _ = _resolve(tokens, year, month)
        score = sum(1 for entry in entries if entry.date is not None and entry.weekday_ok)
        scores.append((score, (year, month)))

    scores.sort(key=lambda item: item[0], reverse=True)
    top_score, _ = scores[0]
    if top_score == 0:
        return None
    tied = [base for score, base in scores if score == top_score]
    if len(tied) > 1:
        return None
    return tied[0]


def _month_candidates(today: date) -> list[tuple[int, int]]:
    result = []
    for offset in (-1, 0, 1):
        month = today.month + offset
        year = today.year
        if month < 1:
            month += 12
            year -= 1
        elif month > 12:
            month -= 12
            year += 1
        result.append((year, month))
    return result


# ---------------------------------------------------------------------------
# date resolution
# ---------------------------------------------------------------------------


def _resolve(tokens: list[tuple], year: int, month: int) -> tuple[list[ParsedLine], list[str]]:
    """Walk the tokens, applying rollover and header rules, building entries."""
    entries: list[ParsedLine] = []
    ignored_dates: list[str] = []
    prev_day: int | None = None
    prev_month: int | None = None

    for token in tokens:
        if token[0] == "h":
            new_month = int(token[1])
            if prev_month is not None and new_month < prev_month:
                year += 1
            month = new_month
            prev_month = month
            prev_day = None
            continue

        _, day, weekday, remainder, raw = token
        # Month advances by one whenever a day number is smaller than the
        # previous entry's -- "31 sabtu" then "2 senin" is next month, not a
        # second weekday spell for the same month.
        if prev_day is not None and day < prev_day:
            month += 1
            if month > 12:
                year += 1
                month = 1
            prev_month = month
        prev_day = day

        try:
            shift_date = date(year, month, day)
        except ValueError:
            entries.append(
                ParsedLine(
                    day=day,
                    date=None,
                    written_day=weekday,
                    remainder=remainder,
                    raw=raw,
                    status="ignored",
                    warnings=["tanggal tidak valid"],
                )
            )
            ignored_dates.append(raw)
            continue

        entry = ParsedLine(
            day=day,
            date=shift_date,
            written_day=weekday,
            remainder=remainder,
            raw=raw,
            weekday_ok=shift_date.weekday() == DAYS[weekday],
        )
        if not entry.weekday_ok:
            entry.warnings.append(f"tanggal {shift_date:%d/%m} bukan hari {weekday}")

        classified = _classify(remainder)
        if classified is None:
            entry.status = "empty"
            entry.warnings.append("belum ada jadwal")
        else:
            kind, kind_ambiguous, place, note, partners = classified
            entry.kind = kind
            entry.kind_ambiguous = kind_ambiguous
            entry.place = place
            entry.note = note
            entry.partners = partners
            if kind is None:
                entry.warnings.append("jam shift tidak bisa dikenali")
            elif kind_ambiguous:
                entry.warnings.append("waktu shift ambigu")
        entries.append(entry)

    return entries, ignored_dates


def _classify(remainder: str) -> tuple[str | None, bool, str | None, str | None, list[str]] | None:
    """Turn the text after the weekday into kind/place/note/partners.

    Returns None when the remainder is empty ("belum ada jadwal").
    """
    partners: list[str] = []
    note_parts: list[str] = []
    groups = GROUP_RE.findall(remainder)
    rest = GROUP_RE.sub("", remainder)
    for group in groups:
        text = group.strip()
        if text.startswith("+"):
            partners.extend(_parse_names(text[1:]))
        elif text:
            note_parts.append(text.lower())

    words = [w.lower() for w in WORD_RE.findall(rest)]
    if not words:
        return None

    kind: str | None = None
    kind_ambiguous = False
    if "libur" in words:
        kind = "libur"
    else:
        times = sorted({TIME_WORDS[w] for w in words if w in TIME_WORDS})
        if times:
            kind = times[0]
            kind_ambiguous = len(times) > 1

    place = next((place_name for place_name in PLACES if place_name in words), None)
    skip = set(TIME_WORDS) | set(PLACES) | {"libur"}
    leftover = [w for w in words if w not in skip]
    note_parts.extend(leftover)
    note = " ".join(dict.fromkeys(note_parts)) or None
    return kind, kind_ambiguous, place, note, partners


def _parse_names(text: str) -> list[str]:
    """Partners split on ',' and '&' and the standalone word 'dan'."""
    parts = re.split(r"[,&]|\s+dan\s+", text, flags=re.IGNORECASE)
    names = []
    for part in parts:
        name = part.strip().title()
        if name:
            names.append(name)
    return names
