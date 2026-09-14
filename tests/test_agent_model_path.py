"""The model-driven agent loop, exercised against a stubbed SDK.

No API key is needed to check the part that actually matters: that the loop
executes the tools the model asks for, feeds their results back in the shape the
Messages API expects, stops when the model stops, and gives up rather than
looping forever.

The stub records every request, so these tests assert on what the agent *sent*,
not only on what it returned.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from searchiq.agent import agent as agent_module


class Block:
    """A content block, shaped like the ones the SDK returns."""

    def __init__(self, type: str, **fields: Any) -> None:
        self.type = type
        for name, value in fields.items():
            setattr(self, name, value)


class Response:
    def __init__(self, stop_reason: str, content: list[Block]) -> None:
        self.stop_reason = stop_reason
        self.content = content


def text_turn(message: str) -> Response:
    return Response("end_turn", [Block("text", text=message)])


def tool_turn(*calls: tuple[str, dict[str, Any]]) -> Response:
    return Response(
        "tool_use",
        [
            Block("tool_use", id=f"call_{index}", name=name, input=arguments)
            for index, (name, arguments) in enumerate(calls)
        ],
    )


class FakeMessages:
    """Returns scripted responses and remembers how it was called."""

    def __init__(self, script: list[Response], *, reject_kwargs: bool = False) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.reject_kwargs = reject_kwargs

    def create(self, **kwargs: Any) -> Response:
        # Mimics an SDK build that does not know the newer parameters.
        if self.reject_kwargs and ("betas" in kwargs or "fallbacks" in kwargs):
            raise TypeError("unexpected keyword argument 'fallbacks'")
        self.requests.append(kwargs)
        if not self.script:
            raise AssertionError("the agent made more requests than the script allows")
        return self.script.pop(0)


class FakeClient:
    def __init__(self, script: list[Response], *, beta_rejects: bool = False) -> None:
        self.messages = FakeMessages(script)
        # The agent prefers the beta endpoint and falls back to the stable one,
        # so both share a script and either may be the one that serves it.
        self.beta = types.SimpleNamespace(
            messages=FakeMessages(script, reject_kwargs=beta_rejects)
        )
        if not beta_rejects:
            self.beta.messages.script = self.messages.script


@pytest.fixture
def live_agent(connection, monkeypatch):
    """Make `ask` take the model path, with a stub standing in for the SDK."""
    from searchiq.config import settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-a-real-credential")
    settings.cache_clear()

    def install(script: list[Response], *, beta_rejects: bool = False) -> FakeClient:
        client = FakeClient(script, beta_rejects=beta_rejects)
        fake_sdk = types.ModuleType("anthropic")
        fake_sdk.Anthropic = lambda **kwargs: client  # noqa: ARG005
        monkeypatch.setitem(sys.modules, "anthropic", fake_sdk)
        return client

    yield install
    settings.cache_clear()


class TestModelLoop:
    def test_an_answer_with_no_tools_is_returned_as_is(self, connection, live_agent) -> None:
        live_agent([text_turn("Search health is 64.4 out of 100.")])

        answer = agent_module.ask(connection, "How is search doing?")

        assert answer.source == "model"
        assert answer.answer == "Search health is 64.4 out of 100."
        assert answer.truncated is False

    def test_a_requested_tool_is_executed_and_its_result_returned(
        self, connection, live_agent
    ) -> None:
        client = live_agent([
            tool_turn(("get_search_health", {})),
            text_turn("Ten searches, six distinct queries."),
        ])

        answer = agent_module.ask(connection, "How is search doing?")

        assert [call.name for call in answer.tool_calls] == ["get_search_health"]
        # The second request must carry the tool result back to the model.
        second = client.beta.messages.requests[1]
        results = second["messages"][-1]["content"]
        assert results[0]["type"] == "tool_result"
        assert results[0]["tool_use_id"] == "call_0"
        assert "total_searches" in results[0]["content"]

    def test_parallel_tool_calls_return_in_one_user_message(
        self, connection, live_agent
    ) -> None:
        # Splitting results across messages teaches the model to stop asking for
        # parallel calls, so they must come back together.
        client = live_agent([
            tool_turn(("get_search_health", {}), ("describe_dataset", {})),
            text_turn("Both read."),
        ])

        agent_module.ask(connection, "How is search doing?")

        results = client.beta.messages.requests[1]["messages"][-1]["content"]
        assert len(results) == 2
        assert {block["tool_use_id"] for block in results} == {"call_0", "call_1"}

    def test_arguments_reach_the_tool(self, connection, live_agent) -> None:
        live_agent([
            tool_turn(("check_catalogue_coverage", {"term": "حليب"})),
            text_turn("Four products."),
        ])

        answer = agent_module.ask(connection, "Do we stock milk?")

        assert answer.tool_calls[0].arguments == {"term": "حليب"}
        assert "exact_product_count" in answer.tool_calls[0].result_preview

    def test_a_tool_error_is_handed_back_rather_than_raised(
        self, connection, live_agent
    ) -> None:
        # A mis-chosen tool must be something the model can read and recover
        # from, not something that kills the turn.
        client = live_agent([
            tool_turn(("no_such_tool", {})),
            text_turn("Trying something else."),
        ])

        answer = agent_module.ask(connection, "anything")

        assert answer.answer == "Trying something else."
        results = client.beta.messages.requests[1]["messages"][-1]["content"]
        assert "unknown tool" in results[0]["content"]

    def test_the_call_budget_is_enforced(self, connection, live_agent) -> None:
        # A model that never stops asking for tools must not spend forever.
        live_agent([tool_turn(("get_search_health", {})) for _ in range(10)])

        answer = agent_module.ask(connection, "loop please", max_iterations=3)

        assert answer.truncated is True
        assert len(answer.tool_calls) == 3
        assert "Stopped after 3 tool calls" in answer.answer

    def test_a_refusal_is_reported_not_swallowed(self, connection, live_agent) -> None:
        live_agent([Response("refusal", [])])

        answer = agent_module.ask(connection, "something declined")

        assert "declined" in answer.answer
        assert answer.source == "model"

    def test_every_call_is_recorded_in_the_trace(self, connection, live_agent) -> None:
        live_agent([
            tool_turn(("describe_dataset", {})),
            tool_turn(("list_problem_queries", {"limit": 3})),
            text_turn("Done."),
        ])

        answer = agent_module.ask(connection, "what needs attention?")

        assert [call.name for call in answer.tool_calls] == [
            "describe_dataset",
            "list_problem_queries",
        ]
        assert all(call.result_preview for call in answer.tool_calls)


class TestRequestShape:
    def test_tools_and_system_prompt_are_sent(self, connection, live_agent) -> None:
        client = live_agent([text_turn("ok")])

        agent_module.ask(connection, "How is search doing?")

        request = client.beta.messages.requests[0]
        assert request["model"] == "claude-opus-5"
        assert request["thinking"] == {"type": "adaptive"}
        assert "search-quality analyst" in request["system"]
        names = {tool["name"] for tool in request["tools"]}
        assert "get_search_health" in names
        assert "check_catalogue_coverage" in names

    def test_the_tool_list_order_is_stable(self, connection, live_agent) -> None:
        # The tool list is part of the cached request prefix; reshuffling it
        # would invalidate the prompt cache on every call.
        client = live_agent([text_turn("a"), text_turn("b")])
        agent_module.ask(connection, "first question")
        client.beta.messages.script.append(text_turn("b"))
        agent_module.ask(connection, "second question")

        first, second = client.beta.messages.requests[:2]
        assert [t["name"] for t in first["tools"]] == [t["name"] for t in second["tools"]]

    def test_it_falls_back_when_the_sdk_rejects_the_beta_parameters(
        self, connection, live_agent
    ) -> None:
        # An SDK build without server-side fallbacks must not take the agent
        # down; the analytics behaviour is identical either way.
        client = live_agent([text_turn("answered anyway")], beta_rejects=True)

        answer = agent_module.ask(connection, "How is search doing?")

        assert answer.answer == "answered anyway"
        assert client.beta.messages.requests == []   # beta refused
        assert len(client.messages.requests) == 1    # stable endpoint served it
        assert "fallbacks" not in client.messages.requests[0]


class TestPathSelection:
    def test_without_a_key_the_offline_planner_answers(self, connection) -> None:
        from searchiq.config import settings

        settings.cache_clear()
        answer = agent_module.ask(connection, "How is search doing?")
        assert answer.source == "deterministic"
        assert answer.model is None

    def test_both_paths_read_the_same_tools(
        self, connection, live_agent, monkeypatch
    ) -> None:
        # The guarantee the dashboard makes: plainer wording, identical numbers.
        live_agent([
            tool_turn(("get_search_health", {})),
            text_turn("Health is 64.4."),
        ])
        model_answer = agent_module.ask(connection, "How is search doing?")

        # Take the key away so the same question falls to the offline planner.
        from searchiq.config import settings

        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        settings.cache_clear()
        offline_answer = agent_module.ask(connection, "How is search doing?")

        assert model_answer.source == "model"
        assert offline_answer.source == "deterministic"
        assert [c.name for c in model_answer.tool_calls] == \
               [c.name for c in offline_answer.tool_calls] == ["get_search_health"]
