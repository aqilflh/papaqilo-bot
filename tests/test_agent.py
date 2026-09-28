"""The agent tool-calling loop, with the LLM replaced by a scripted fake.

The point of these tests is the control flow: a tool call becomes a tool
message, the loop repeats, and the iteration cap is respected. No network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app import db
from app.agent import SYSTEM_PROMPT, Agent, run_and_store

OWNER = 100
FRIEND = 200
WIB = ZoneInfo("Asia/Jakarta")


# --------------------------------------------------------------------------
# a fake OpenAI client
# --------------------------------------------------------------------------


@dataclass
class FakeFunction:
    name: str
    arguments: str


@dataclass
class FakeCall:
    """Mirrors openai's tool call: call.function.name / .arguments."""

    id: str
    function: FakeFunction


@dataclass
class FakeMessage:
    content: str | None = None
    tool_calls: list[FakeCall] | None = None


@dataclass
class FakeUsage:
    prompt_tokens: int = 10
    completion_tokens: int = 5


@dataclass
class FakeResponse:
    """Mirrors the real shape: response.choices[0].message."""

    message: FakeMessage
    usage: FakeUsage = field(default_factory=FakeUsage)
    choices: list[FakeChoice] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.choices:
            self.choices = [FakeChoice(message=self.message)]


@dataclass
class FakeChoice:
    message: FakeMessage


class FakeCompletions:
    """Replays a scripted list of responses and records every request."""

    def __init__(self, script: list[FakeResponse]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("agent made more LLM calls than the script allows")
        return self.script.pop(0)


class FakeClient:
    def __init__(self, script: list[FakeResponse]) -> None:
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeCompletions(script)


class FakeHTTP:
    async def get(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("tests must not hit the network")


def make_agent(script: list[FakeResponse], history: int = 20, cap: int = 6) -> Agent:
    agent = Agent.__new__(Agent)  # bypass the real OpenAI client
    from app.config import Config

    agent.config = Config(
        deepseek_api_key="test",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-flash",
        deepseek_thinking=False,
        telegram_bot_token="test",
        owner_telegram_id=OWNER,
        tz=WIB,
        db_path="data/test.db",
        feeds_path="feeds.json",
        history_messages=history,
        max_tool_iterations=cap,
        max_output_tokens=100,
        default_daily_limit=50,
        tool_timeout=5.0,
        request_timeout=5.0,
        show_thinking=False,
        log_level="WARNING",
    )
    agent.feeds = []
    agent.http = FakeHTTP()
    agent.client = FakeClient(script)
    return agent


@pytest.fixture()
def conn(tmp_path):
    db.connect(tmp_path / "agent.db")
    db.upsert_user(OWNER, "Owner", role="owner", daily_limit=50)
    db.upsert_user(FRIEND, "Friend", role="user", daily_limit=50)
    yield
    db.close()


def script_one_tool_then_answer(tool: str = "get_weather", args: dict | None = None):
    call = FakeCall(
        id="call_1",
        function=FakeFunction(name=tool, arguments=json.dumps(args or {"city": "Jakarta"})),
    )
    return [
        FakeResponse(FakeMessage(content=None, tool_calls=[call])),
        FakeResponse(FakeMessage(content="Jakarta cerah, 31 derajat.")),
    ]


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------


async def test_tool_call_then_final_answer(conn):
    agent = make_agent(script_one_tool_then_answer())
    result = await agent.run(OWNER, "cuaca Jakarta?", owner=True)

    assert result.text == "Jakarta cerah, 31 derajat."
    assert result.tool_calls == 1
    assert len(agent.client.chat.completions.calls) == 2

    # The second request must carry a tool message for the call.
    second = agent.client.chat.completions.calls[1]["messages"]
    tool_messages = [m for m in second if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"


async def test_tokens_are_accumulated_across_iterations(conn):
    agent = make_agent(script_one_tool_then_answer())
    result = await agent.run(OWNER, "cuaca?", owner=True)
    # Two calls at 10 in / 5 out each.
    assert result.tokens_in == 20
    assert result.tokens_out == 10


async def test_no_tool_call_returns_content_directly(conn):
    agent = make_agent([FakeResponse(FakeMessage(content="Halo!"))])
    result = await agent.run(OWNER, "halo", owner=True)
    assert result.text == "Halo!"
    assert result.tool_calls == 0
    assert len(agent.client.chat.completions.calls) == 1


async def test_iteration_cap_stops_the_loop(conn):
    # Every response asks for another tool; the cap must end it.
    endless = [
        FakeResponse(
            FakeMessage(
                content=None,
                tool_calls=[
                    FakeCall(
                        id=f"c{i}",
                        function=FakeFunction(name="get_weather", arguments="{}"),
                    )
                ],
            )
        )
        for i in range(20)
    ]
    agent = make_agent(endless, cap=3)
    result = await agent.run(OWNER, "looping", owner=True)

    assert result.error == "iteration_cap"
    assert "terlalu banyak langkah" in result.text
    # Exactly `cap` LLM calls, not more.
    assert len(agent.client.chat.completions.calls) == 3
    assert result.tool_calls == 3


async def test_tool_error_is_fed_back_not_raised(conn):
    # get_news with no feeds configured fails; the loop must continue anyway.
    script = [
        FakeResponse(
            FakeMessage(
                content=None,
                tool_calls=[
                    FakeCall(id="c1", function=FakeFunction(name="get_news", arguments='{"limit": 5}'))
                ],
            )
        ),
        FakeResponse(FakeMessage(content="Maaf, feed tidak tersedia.")),
    ]
    agent = make_agent(script)
    result = await agent.run(OWNER, "berita apa saja?", owner=True)

    assert result.text == "Maaf, feed tidak tersedia."
    tool_msg = [
        m for m in agent.client.chat.completions.calls[1]["messages"] if m.get("role") == "tool"
    ][0]
    assert "ERROR" in tool_msg["content"]


async def test_malformed_tool_arguments_do_not_crash(conn):
    script = [
        FakeResponse(
            FakeMessage(
                content=None,
                tool_calls=[
                    FakeCall(
                        id="c1", function=FakeFunction(name="get_weather", arguments="{not json")
                    )
                ],
            )
        ),
        FakeResponse(FakeMessage(content="Maaf, tadi bermasalah.")),
    ]
    agent = make_agent(script)
    result = await agent.run(OWNER, "cuaca?", owner=True)
    assert result.text == "Maaf, tadi bermasalah."
    # Malformed JSON becomes {}, so get_weather reports "no city" rather than dying.
    tool_msg = [
        m for m in agent.client.chat.completions.calls[1]["messages"] if m.get("role") == "tool"
    ][0]
    assert "ERROR" in tool_msg["content"]


async def test_llm_error_is_reported_gracefully(conn):
    from openai import APIConnectionError

    agent = make_agent([])

    async def boom(**_kwargs):
        raise APIConnectionError(request=None)

    agent.client.chat.completions.create = boom
    result = await agent.run(OWNER, "halo", owner=True)
    assert "tidak merespons" in result.text
    assert result.error is not None


# --------------------------------------------------------------------------
# prompt shape: the cache-sensitive part
# --------------------------------------------------------------------------


def test_prompt_order_is_system_history_now_user(conn):
    db.add_message(OWNER, "user", "pertanyaan lama")
    db.add_message(OWNER, "assistant", "jawaban lama")
    agent = make_agent([])

    messages = agent.build_messages(OWNER, "pertanyaan baru")

    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == SYSTEM_PROMPT
    assert messages[1]["content"] == "pertanyaan lama"
    assert messages[2]["content"] == "jawaban lama"
    # The clock goes second-to-last, the new user text last.
    assert "Waktu sekarang" in messages[-2]["content"]
    assert messages[-1]["content"] == "pertanyaan baru"


def test_system_prompt_is_identical_between_turns(conn):
    # A cache miss here costs 50x on input tokens, so pin it.
    agent = make_agent([])
    first = agent.build_messages(OWNER, "satu")
    db.add_message(OWNER, "user", "satu")
    db.add_message(OWNER, "assistant", "dua")
    second = agent.build_messages(OWNER, "tiga")
    assert first[0]["content"] == second[0]["content"]


def test_history_is_capped(conn):
    for i in range(40):
        db.add_message(OWNER, "user", f"pesan {i}")
    agent = make_agent([], history=5)
    messages = agent.build_messages(OWNER, "sekarang")
    # 1 system + 5 history + 1 now-note + 1 user
    assert len(messages) == 8


def test_history_is_per_user(conn):
    db.add_message(FRIEND, "user", "rahasia friend")
    agent = make_agent([])
    assert "rahasia friend" not in str(agent.build_messages(OWNER, "hai"))


async def test_thinking_disabled_is_sent_when_off(conn):
    agent = make_agent([FakeResponse(FakeMessage(content="ok"))])
    await agent.run(OWNER, "hai", owner=True)
    assert agent.client.chat.completions.calls[0].get("extra_body") == {
        "thinking": {"type": "disabled"}
    }


async def test_thinking_enabled_sends_no_override(conn):
    agent = make_agent([FakeResponse(FakeMessage(content="ok"))])
    agent.config = replace(agent.config, deepseek_thinking=True)
    await agent.run(OWNER, "hai", owner=True)
    assert "extra_body" not in agent.client.chat.completions.calls[0]


async def test_model_id_is_passed_through(conn):
    agent = make_agent([FakeResponse(FakeMessage(content="ok"))])
    await agent.run(OWNER, "hai", owner=True)
    assert agent.client.chat.completions.calls[0]["model"] == "deepseek-flash"


async def test_tools_are_passed_on_every_call(conn):
    agent = make_agent(script_one_tool_then_answer())
    await agent.run(OWNER, "cuaca?", owner=True)
    for call in agent.client.chat.completions.calls:
        names = {d["function"]["name"] for d in call["tools"]}
        assert "get_weather" in names


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


async def test_run_and_store_persists_both_turns(conn):
    agent = make_agent([FakeResponse(FakeMessage(content="JawabanFinal"))])
    await run_and_store(agent, OWNER, "Pertanyaan", owner=True)

    stored = db.recent_messages(OWNER, 10)
    assert [m["content"] for m in stored] == ["Pertanyaan", "JawabanFinal"]


async def test_run_and_store_counts_usage(conn):
    agent = make_agent([FakeResponse(FakeMessage(content="Halo"))])
    await run_and_store(agent, OWNER, "hi", owner=True)

    usage = db.get_usage(OWNER, db.local_day(WIB))
    assert usage["msg_count"] == 1
    assert usage["tokens_in"] == 10
    assert usage["tokens_out"] == 5


async def test_usage_is_not_shared_between_users(conn):
    agent = make_agent([FakeResponse(FakeMessage(content="Halo"))])
    await run_and_store(agent, OWNER, "hi", owner=True)
    assert db.get_usage(FRIEND, db.local_day(WIB))["msg_count"] == 0


async def test_daily_limit_is_enforced_at_the_db_layer(conn):
    # The gate lives in bot.on_text; here we assert the counter it reads.
    db.set_daily_limit(FRIEND, 2)
    agent = make_agent(
        [
            FakeResponse(FakeMessage(content="satu")),
            FakeResponse(FakeMessage(content="dua")),
        ]
    )
    assert db.usage_remaining(FRIEND, db.local_day(WIB)) == 2
    await run_and_store(agent, FRIEND, "p1", owner=False)
    assert db.usage_remaining(FRIEND, db.local_day(WIB)) == 1
    await run_and_store(agent, FRIEND, "p2", owner=False)
    assert db.usage_remaining(FRIEND, db.local_day(WIB)) == 0
