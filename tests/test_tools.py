"""Tool argument validation, the auth boundary, and the schema shape.

The security-relevant cases are the ones where user_id must not come from the
model, and where a tool must fail closed rather than return something.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import tools
from app.tools import (
    TOOL_BY_NAME,
    TOOLS,
    ToolContext,
    ToolError,
    ValidationError,
    _validate,
    get_news,
    load_feeds,
    run_tool,
    tool_definitions,
)


@pytest.fixture()
def ctx():
    return ToolContext(user_id=7, saved_city=None, http=None, feeds=[])


@pytest.fixture()
def conn(tmp_path):
    from app import db

    db.connect(tmp_path / "tools.db")
    db.upsert_user(7, "Ctx", daily_limit=50)
    yield
    db.close()


# --------------------------------------------------------------------------
# schema shape
# --------------------------------------------------------------------------


def test_every_tool_has_a_schema():
    for tool in TOOLS:
        assert tool.name and tool.description
        assert tool.parameters["type"] == "object"
        assert isinstance(tool.parameters.get("properties"), dict)


def test_tool_definitions_are_in_openai_shape():
    defs = tool_definitions(owner=True)
    assert {d["function"]["name"] for d in defs} == set(TOOL_BY_NAME)
    for d in defs:
        assert d["type"] == "function"
        assert "name" in d["function"]
        assert "description" in d["function"]
        assert "parameters" in d["function"]


def test_tool_definitions_are_stable_for_the_cache_prefix():
    # DeepSeek's prefix cache only pays off if the tools array is identical
    # between calls. Assert two independent calls agree exactly.
    assert tool_definitions(owner=True) == tool_definitions(owner=True)


def test_owner_only_tools_are_hidden_from_non_owners():
    for tool in TOOLS:
        if tool.owner_only:
            assert tool.name not in {d["function"]["name"] for d in tool_definitions(owner=False)}


def test_tool_names_are_unique():
    assert len(TOOL_BY_NAME) == len(TOOLS)


# --------------------------------------------------------------------------
# argument validation
# --------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "count": {"type": "integer"},
        "ratio": {"type": "number"},
        "flag": {"type": "boolean"},
    },
    "required": ["text"],
}


NO_REQUIRED = {"type": "object", "properties": {}}


def test_required_argument_is_enforced():
    with pytest.raises(ValidationError, match="missing required"):
        _validate({}, SCHEMA)


def test_whitespace_only_required_string_is_accepted_by_the_type_checker():
    # _validate checks shape, not semantics. save_note is the layer that rejects
    # an empty note, so the rule lives in exactly one place.
    assert _validate({"text": "   "}, SCHEMA) == {"text": "   "}


def test_wrong_type_is_rejected():
    with pytest.raises(ValidationError, match="must be a string"):
        _validate({"text": 42}, SCHEMA)
    with pytest.raises(ValidationError, match="must be an integer"):
        _validate({"text": "x", "count": "banyak"}, SCHEMA)


def test_bool_is_not_accepted_as_an_integer():
    # isinstance(True, int) is True in Python, so this needs an explicit guard.
    with pytest.raises(ValidationError, match="must be an integer"):
        _validate({"text": "x", "count": True}, SCHEMA)


def test_bool_is_not_accepted_as_a_number():
    with pytest.raises(ValidationError, match="must be a number"):
        _validate({"text": "x", "ratio": False}, SCHEMA)


def test_number_accepts_int_and_float():
    assert _validate({"text": "x", "ratio": 3}, SCHEMA)["ratio"] == 3
    assert _validate({"text": "x", "ratio": 3.5}, SCHEMA)["ratio"] == 3.5


def test_unknown_keys_are_ignored_not_fatal():
    # The model invents extra keys; that must not break the call.
    assert _validate({"text": "x", "ngarang": 1}, SCHEMA) == {"text": "x"}


def test_none_optional_is_dropped():
    assert _validate({"text": "x", "count": None}, SCHEMA) == {"text": "x"}


def test_non_object_arguments_are_rejected():
    with pytest.raises(ValidationError, match="must be an object"):
        _validate(["a", "b"], SCHEMA)
    with pytest.raises(ValidationError, match="must be an object"):
        _validate("halo", SCHEMA)


def test_none_arguments_become_empty_object():
    assert _validate(None, NO_REQUIRED) == {}


def test_none_arguments_still_enforce_required():
    with pytest.raises(ValidationError, match="missing required"):
        _validate(None, SCHEMA)


# --------------------------------------------------------------------------
# the user_id boundary
# --------------------------------------------------------------------------


def test_no_tool_takes_a_user_id_parameter():
    # If a tool accepted user_id, the model could target another user.
    for tool in TOOLS:
        assert "user_id" not in tool.parameters.get("properties", {})
        assert "user_id" not in tool.parameters.get("required", [])


async def test_save_note_uses_the_context_user_not_the_arguments(ctx, conn):
    # A model trying to write into someone else's notebook has no way to say so.
    from app import db

    out = await run_tool("save_note", {"text": "catatan saya", "user_id": 999}, ctx, owner=True)
    assert "ERROR" not in out
    assert ctx.user_id == 7

    # It landed on the context user, and nowhere near 999.
    assert [n["content"] for n in db.list_notes(7)] == ["catatan saya"]
    assert db.list_notes(999) == []


async def test_unknown_tool_returns_error_not_crash(ctx):
    out = await run_tool("rm_rf", {}, ctx, owner=True)
    assert "tidak dikenal" in out


async def test_list_notes_query_argument_must_be_text(ctx, conn):
    out = await run_tool("list_notes", {"query": 123}, ctx, owner=True)
    assert "ERROR" in out
    # The generic type check in _validate catches this before the tool body.
    assert "must be a string" in out


async def test_save_note_rejects_empty_text(ctx, conn):
    out = await run_tool("save_note", {"text": "   "}, ctx, owner=True)
    assert "ERROR" in out
    assert "tidak boleh kosong" in out


async def test_owner_only_tool_is_refused_for_non_owners(ctx):
    # No tool is owner_only today, so register one temporarily to prove the gate.
    async def ok(_ctx, _args):
        return "ok"

    tools.TOOL_BY_NAME["danger_test"] = tools.Tool(
        name="danger_test",
        description="test only",
        parameters={"type": "object", "properties": {}},
        handler=ok,
        owner_only=True,
    )
    try:
        denied = await run_tool("danger_test", {}, ctx, owner=False)
        allowed = await run_tool("danger_test", {}, ctx, owner=True)
    finally:
        del tools.TOOL_BY_NAME["danger_test"]

    assert "hanya untuk owner" in denied
    assert allowed == "ok"


async def test_tool_exception_becomes_an_error_string_not_a_crash(ctx):
    async def boom(_ctx, _args):
        raise RuntimeError("kaboom")

    tools.TOOL_BY_NAME["boom_test"] = tools.Tool(
        name="boom_test",
        description="test only",
        parameters={"type": "object", "properties": {}},
        handler=boom,
    )
    try:
        out = await run_tool("boom_test", {}, ctx, owner=True)
    finally:
        del tools.TOOL_BY_NAME["boom_test"]
    assert "ERROR" in out
    assert "RuntimeError" in out


async def test_tool_timeout_is_reported_as_an_error(ctx):
    async def slow(_ctx, _args):
        import asyncio

        await asyncio.sleep(5)
        return "tidak seharusnya sampai"

    tools.TOOL_BY_NAME["slow_test"] = tools.Tool(
        name="slow_test",
        description="test only",
        parameters={"type": "object", "properties": {}},
        handler=slow,
    )
    try:
        out = await run_tool("slow_test", {}, ctx, owner=True, seconds=0.05)
    finally:
        del tools.TOOL_BY_NAME["slow_test"]
    assert "batas waktu" in out


# --------------------------------------------------------------------------
# clamps and truncation
# --------------------------------------------------------------------------


def test_tool_output_is_truncated_to_a_bound():
    body = "b" * (tools.MAX_TOOL_OUTPUT * 3)
    out = tools._truncate(body)
    assert len(out) <= tools.MAX_TOOL_OUTPUT + 60
    assert "dipotong" in out


def test_short_output_is_not_touched():
    assert tools._truncate("pendek") == "pendek"


def test_limit_values_are_clamped():
    assert tools._clamp(999, 1, 20) == 20
    assert tools._clamp(-5, 1, 20) == 1
    assert tools._clamp(7, 1, 20) == 7


# --------------------------------------------------------------------------
# weather code descriptions
# --------------------------------------------------------------------------


def test_wmo_codes_map_to_indonesian():
    assert tools.describe_code(0) == "cerah"
    assert tools.describe_code(95) == "badai petir"
    assert tools.describe_code(61) == "hujan ringan"


def test_unknown_code_does_not_crash():
    assert "999" in tools.describe_code(999)
    assert tools.describe_code(None) == "tidak diketahui"


# --------------------------------------------------------------------------
# feeds.json
# --------------------------------------------------------------------------


def test_load_feeds_reads_the_real_file():
    feeds = load_feeds("feeds.json")
    assert len(feeds) >= 1
    assert all("name" in f and "url" in f for f in feeds)
    assert all(f["url"].startswith("http") for f in feeds)


def test_load_feeds_survives_a_missing_file(tmp_path):
    assert load_feeds(tmp_path / "nope.json") == []


def test_load_feeds_survives_invalid_json(tmp_path):
    bad = tmp_path / "feeds.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_feeds(bad) == []


def test_load_feeds_drops_entries_without_a_url(tmp_path):
    f = tmp_path / "feeds.json"
    f.write_text(json.dumps({"feeds": [{"name": "x"}, {"name": "y", "url": "http://a"}]}))
    assert load_feeds(f) == [{"name": "y", "url": "http://a"}]


# --------------------------------------------------------------------------
# get_news
# --------------------------------------------------------------------------


def _rss(items: list[tuple[str, str, str]]) -> str:
    """Build a minimal RSS document from (title, link, pubDate) triples."""
    entries = "".join(
        f"<item><title>{title}</title><link>{link}</link><pubDate>{published}</pubDate></item>"
        for title, link, published in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{entries}</channel></rss>'


FEED_A = "http://a.example/rss"
FEED_B = "http://b.example/rss"


def _news_client(bodies: dict[str, str]):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=bodies[str(request.url)])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_news_limit_is_a_total_budget_not_a_per_feed_quota():
    # The bug this guards: asking for 3 headlines used to return 3 from *each*
    # feed, so "3" quietly meant 6.
    bodies = {
        FEED_A: _rss(
            [
                ("A1", "http://a/1", "Mon, 28 Sep 2026 10:00:00 +0700"),
                ("A2", "http://a/2", "Mon, 28 Sep 2026 09:00:00 +0700"),
                ("A3", "http://a/3", "Mon, 28 Sep 2026 08:00:00 +0700"),
            ]
        ),
        FEED_B: _rss(
            [
                ("B1", "http://b/1", "Mon, 28 Sep 2026 11:00:00 +0700"),
                ("B2", "http://b/2", "Mon, 28 Sep 2026 07:00:00 +0700"),
            ]
        ),
    }
    ctx = ToolContext(
        user_id=7,
        saved_city=None,
        http=_news_client(bodies),
        feeds=[{"name": "A", "url": FEED_A}, {"name": "B", "url": FEED_B}],
    )
    out = await get_news(ctx, {"limit": 3})

    headlines = [line for line in out.splitlines() if line.startswith("- ")]
    assert len(headlines) == 3
    # Newest three across both feeds, not newest three of A.
    titles = " ".join(headlines)
    assert "B1" in titles
    assert "A1" in titles
    assert "A2" in titles
    assert "A3" not in titles  # older than the cutoff
    assert "B2" not in titles
    await ctx.http.aclose()


async def test_news_orders_headlines_newest_first_across_feeds():
    bodies = {
        FEED_A: _rss([("A-old", "http://a/1", "Mon, 28 Sep 2026 01:00:00 +0700")]),
        FEED_B: _rss([("B-new", "http://b/1", "Mon, 28 Sep 2026 23:00:00 +0700")]),
    }
    ctx = ToolContext(
        user_id=7,
        saved_city=None,
        http=_news_client(bodies),
        feeds=[{"name": "A", "url": FEED_A}, {"name": "B", "url": FEED_B}],
    )
    out = await get_news(ctx, {})
    headlines = [line for line in out.splitlines() if line.startswith("- ")]

    assert [h.split()[1] for h in headlines] == ["B-new", "A-old"]
    # Grouped by source, in the order the freshest items appear.
    assert out.index("[B]") < out.index("[A]")
    await ctx.http.aclose()


async def test_one_broken_feed_does_not_sink_the_others():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == FEED_A:
            return httpx.Response(503)
        return httpx.Response(
            200, text=_rss([("B1", "http://b/1", "Mon, 28 Sep 2026 10:00:00 +0700")])
        )

    ctx = ToolContext(
        user_id=7,
        saved_city=None,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        feeds=[{"name": "A", "url": FEED_A}, {"name": "B", "url": FEED_B}],
    )
    out = await get_news(ctx, {})

    assert "B1" in out
    assert "GAGAL diambil" in out
    assert "Catatan:" in out
    await ctx.http.aclose()


async def test_news_survives_a_feed_with_no_usable_titles():
    bodies = {
        FEED_A: _rss([("", "http://a/1", "Mon, 28 Sep 2026 10:00:00 +0700")]),
        FEED_B: _rss([("B1", "http://b/1", "Mon, 28 Sep 2026 10:00:00 +0700")]),
    }
    ctx = ToolContext(
        user_id=7,
        saved_city=None,
        http=_news_client(bodies),
        feeds=[{"name": "A", "url": FEED_A}, {"name": "B", "url": FEED_B}],
    )
    out = await get_news(ctx, {})

    assert "B1" in out
    assert "http://a/1" not in out
    await ctx.http.aclose()


async def test_news_reports_an_unknown_feed_name():
    ctx = ToolContext(
        user_id=7,
        saved_city=None,
        http=_news_client({}),
        feeds=[{"name": "Antara News", "url": FEED_A}],
    )
    with pytest.raises(ToolError, match="tidak dikenal"):
        await get_news(ctx, {"feed_names": ["Detik"]})
    await ctx.http.aclose()


def test_route_eta_without_api_key_says_so(conn):
    out = asyncio.run(
        run_tool(
            "route_eta",
            {"name": "Pulang"},
            ToolContext(user_id=7, saved_city=None, http=None, feeds=[]),
            owner=True,
        )
    )
    assert "belum aktif" in out
    assert "GOOGLE_MAPS_API_KEY" in out


def test_set_reminder_keeps_a_monthly_pattern(ctx, conn):
    """'Tiap tanggal 1' must be stored as monthly, with the day the user asked for."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from app import db

    wib = ZoneInfo("Asia/Jakarta")
    first = (datetime.now(wib) + timedelta(days=60)).replace(
        day=1, hour=9, minute=0, second=0, microsecond=0
    )

    out = asyncio.run(
        tools.set_reminder(
            ctx,
            {
                "text": "jalankan prepaid switching",
                "at": first.strftime("%Y-%m-%dT%H:%M"),
                "repeat": "monthly",
            },
        )
    )

    row = db.list_reminders(ctx.user_id)[0]
    assert row["repeat_kind"] == "monthly"
    assert row["monthday"] == 1
    assert "setiap tanggal 1" in out
