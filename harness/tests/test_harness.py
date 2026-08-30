"""Tests for the provider-neutral core.

Standard-library unittest so the test run needs nothing beyond the app's own
dependencies. Run from the repo root:

    python harness/tests/test_harness.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

from agent import Agent  # noqa: E402
from config import RouteConfig, Settings  # noqa: E402
from llm import Router, split_ref  # noqa: E402
from memory import Memory, _repair  # noqa: E402
from provider_anthropic import AnthropicProvider  # noqa: E402
from provider_base import (  # noqa: E402
    Completion,
    Message,
    ProviderError,
    Text,
    ToolResult,
    ToolSpec,
    ToolUse,
    Usage,
)
from provider_google import GoogleProvider, _clean_schema  # noqa: E402
from provider_openai import OpenAICompatProvider  # noqa: E402
from tool_registry import ToolRegistry, obj, string  # noqa: E402


class StubProvider:
    """Returns a scripted sequence of Completions and records what it was sent."""

    def __init__(self, script, slug="stub"):
        self.script = list(script)
        self.slug = slug
        self.calls = []

    async def complete(self, **kw):
        self.calls.append(kw)
        result = self.script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class StubGateway:
    """Captures the request body an adapter builds, without any network."""

    def __init__(self, response=None):
        self.response = response or {}
        self.last = None

    async def post_json(self, provider, path, *, headers, json, cache=True):
        self.last = {"provider": provider, "path": path, "headers": headers, "json": json}
        return self.response


# ---------------------------------------------------------------------------


class TestModelRefs(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(split_ref("anthropic/claude-opus-5"), ("anthropic", "claude-opus-5"))

    def test_openrouter_keeps_its_own_slashes(self):
        self.assertEqual(
            split_ref("openrouter/meta-llama/llama-3.3-70b-instruct"),
            ("openrouter", "meta-llama/llama-3.3-70b-instruct"),
        )

    def test_workers_ai_model_id(self):
        self.assertEqual(
            split_ref("workers-ai/@cf/meta/llama-3.3-70b-instruct-fp8-fast"),
            ("workers-ai", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"),
        )

    def test_missing_provider_is_rejected(self):
        with self.assertRaises(ProviderError):
            split_ref("claude-opus-5")


class TestRouterFallback(unittest.IsolatedAsyncioTestCase):
    def _settings(self):
        os.environ["HARNESS_ROUTES"] = "/nonexistent"
        return Settings()

    async def test_falls_back_when_primary_is_retryable(self):
        primary = StubProvider([ProviderError("rate limited", status=429, retryable=True)])
        fallback = StubProvider([Completion(text="from the fallback")])
        router = Router(self._settings(), {"a": primary, "b": fallback})

        result = await router.complete(
            RouteConfig(primary="a/m1", fallback="b/m2"),
            system="s", messages=[Message.user("hi")], tools=[],
        )
        self.assertEqual(result.text, "from the fallback")
        self.assertEqual(len(fallback.calls), 1)

    async def test_does_not_fall_back_on_a_bad_request(self):
        # A 400 means we built the request wrong; the fallback would fail too.
        primary = StubProvider([ProviderError("bad request", status=400, retryable=False)])
        fallback = StubProvider([Completion(text="should not be reached")])
        router = Router(self._settings(), {"a": primary, "b": fallback})

        with self.assertRaises(ProviderError):
            await router.complete(
                RouteConfig(primary="a/m1", fallback="b/m2"),
                system="s", messages=[Message.user("hi")], tools=[],
            )
        self.assertEqual(fallback.calls, [])

    async def test_unknown_provider_is_skipped_not_fatal(self):
        fallback = StubProvider([Completion(text="ok")])
        router = Router(self._settings(), {"b": fallback})
        result = await router.complete(
            RouteConfig(primary="ghost/m1", fallback="b/m2"),
            system="s", messages=[Message.user("hi")], tools=[],
        )
        self.assertEqual(result.text, "ok")


class TestAgentLoop(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        os.environ["HARNESS_ROUTES"] = "/nonexistent"
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.settings.db_path = str(Path(self.tmp.name) / "t.db")
        self.memory = Memory(self.settings.db_path)
        self.registry = ToolRegistry()
        self.invoked = []

        async def fake_light(entity_id: str):
            self.invoked.append(entity_id)
            return "off"

        self.registry.add(
            "ha_call_service", "turn things off",
            obj({"entity_id": string("id")}, ["entity_id"]), fake_light,
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _agent(self, script):
        provider = StubProvider(script)
        router = Router(self.settings, {"stub": provider})
        self.settings._raw_routes = {
            "routes": {"chat": {"primary": "stub/m", "fallback": ""}}
        }
        agent = Agent(
            settings=self.settings, router=router,
            registry=self.registry, memory=self.memory,
        )
        return agent, provider

    async def test_tool_call_then_answer(self):
        agent, provider = self._agent([
            Completion(
                text="", stop_reason="tool_use",
                tool_calls=[ToolUse("t1", "ha_call_service", {"entity_id": "light.kitchen"})],
                usage=Usage(10, 5),
            ),
            Completion(text="Kitchen light is off.", usage=Usage(20, 8)),
        ])
        result = await agent.run("turn off the kitchen light", session_id="s1")

        self.assertEqual(result.text, "Kitchen light is off.")
        self.assertEqual(self.invoked, ["light.kitchen"])
        # Usage accumulates across every hop of the loop.
        self.assertEqual(result.usage.input_tokens, 30)
        self.assertEqual(result.usage.output_tokens, 13)
        self.assertEqual(result.tool_calls[0]["name"], "ha_call_service")
        self.assertTrue(result.tool_calls[0]["ok"])

        # The second request must carry the tool result back to the model.
        second = provider.calls[1]["messages"]
        results = [b for m in second for b in m.content if isinstance(b, ToolResult)]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].content, "off")

    async def test_tool_failure_is_reported_to_the_model_not_raised(self):
        async def boom():
            raise RuntimeError("HA unreachable")

        self.registry.add("broken", "fails", obj({}), boom)
        agent, provider = self._agent([
            Completion(text="", stop_reason="tool_use",
                       tool_calls=[ToolUse("t1", "broken", {})]),
            Completion(text="I could not reach Home Assistant."),
        ])
        result = await agent.run("do it", session_id="s2")

        self.assertIn("could not reach", result.text)
        self.assertFalse(result.tool_calls[0]["ok"])
        second = provider.calls[1]["messages"]
        errors = [b for m in second for b in m.content
                  if isinstance(b, ToolResult) and b.is_error]
        self.assertEqual(len(errors), 1)
        self.assertIn("HA unreachable", errors[0].content)

    async def test_unknown_tool_does_not_kill_the_turn(self):
        agent, _ = self._agent([
            Completion(text="", stop_reason="tool_use",
                       tool_calls=[ToolUse("t1", "no_such_tool", {})]),
            Completion(text="That is not something I can do."),
        ])
        result = await agent.run("x", session_id="s3")
        self.assertEqual(result.text, "That is not something I can do.")
        self.assertFalse(result.tool_calls[0]["ok"])

    async def test_parallel_tool_calls_return_in_one_message(self):
        agent, provider = self._agent([
            Completion(
                text="", stop_reason="tool_use",
                tool_calls=[
                    ToolUse("t1", "ha_call_service", {"entity_id": "light.a"}),
                    ToolUse("t2", "ha_call_service", {"entity_id": "light.b"}),
                ],
            ),
            Completion(text="Both off."),
        ])
        await agent.run("both", session_id="s4")

        self.assertEqual(sorted(self.invoked), ["light.a", "light.b"])
        second = provider.calls[1]["messages"]
        result_msgs = [m for m in second
                       if any(isinstance(b, ToolResult) for b in m.content)]
        self.assertEqual(len(result_msgs), 1, "results must be batched into one message")
        self.assertEqual(len(result_msgs[0].content), 2)

    async def test_iteration_limit_is_enforced(self):
        self.settings.max_tool_iterations = 3
        loop = [
            Completion(text="", stop_reason="tool_use",
                       tool_calls=[ToolUse(f"t{i}", "ha_call_service",
                                           {"entity_id": "light.x"})])
            for i in range(3)
        ]
        agent, provider = self._agent(loop)
        result = await agent.run("spin", session_id="s5")
        self.assertEqual(len(provider.calls), 3)
        self.assertIn("narrow down", result.text)

    async def test_history_persists_across_turns(self):
        agent, provider = self._agent([
            Completion(text="Hello."),
            Completion(text="Still here."),
        ])
        await agent.run("hi", session_id="s6")
        await agent.run("again", session_id="s6")

        second = provider.calls[1]["messages"]
        # first user, first assistant, second user
        self.assertEqual(len(second), 3)
        self.assertEqual(second[0].content[0].text, "hi")
        self.assertEqual(second[1].role, "assistant")


class TestMemoryRepair(unittest.TestCase):
    def test_drops_leading_orphan_tool_result(self):
        msgs = [
            Message(role="user", content=[ToolResult("t1", "orphan")]),
            Message(role="assistant", content=[Text("a")]),
            Message(role="user", content=[Text("b")]),
        ]
        repaired = _repair(msgs)
        self.assertEqual(len(repaired), 1)
        self.assertEqual(repaired[0].content[0].text, "b")

    def test_drops_trailing_unanswered_tool_use(self):
        msgs = [
            Message(role="user", content=[Text("q")]),
            Message(role="assistant", content=[ToolUse("t1", "x", {})]),
        ]
        repaired = _repair(msgs)
        self.assertEqual(len(repaired), 1)
        self.assertEqual(repaired[0].role, "user")

    def test_leaves_a_clean_history_alone(self):
        msgs = [
            Message(role="user", content=[Text("q")]),
            Message(role="assistant", content=[Text("a")]),
        ]
        self.assertEqual(len(_repair(list(msgs))), 2)


class TestAnthropicAdapter(unittest.IsolatedAsyncioTestCase):
    async def test_builds_native_messages_request(self):
        gw = StubGateway({
            "content": [
                {"type": "text", "text": "hi there"},
                {"type": "tool_use", "id": "tu1", "name": "f", "input": {"a": 1}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 7, "output_tokens": 3},
            "model": "claude-opus-5",
        })
        p = AnthropicProvider(gw, "sk-test")
        out = await p.complete(
            model="claude-opus-5", system="be brief",
            messages=[Message.user("hello")],
            tools=[ToolSpec("f", "does f", {"type": "object", "properties": {}})],
            max_tokens=100, temperature=0.5,
        )

        body = gw.last["json"]
        self.assertEqual(gw.last["path"], "v1/messages")
        self.assertEqual(gw.last["headers"]["x-api-key"], "sk-test")
        self.assertEqual(gw.last["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(body["system"], "be brief")
        # Anthropic names the tool schema field input_schema.
        self.assertIn("input_schema", body["tools"][0])
        # Opus 5 rejects sampling params, so they must be dropped silently.
        self.assertNotIn("temperature", body)

        self.assertEqual(out.text, "hi there")
        self.assertEqual(out.tool_calls[0].name, "f")
        self.assertEqual(out.usage.input_tokens, 7)

    async def test_keeps_temperature_for_a_model_that_accepts_it(self):
        gw = StubGateway({"content": [{"type": "text", "text": "x"}],
                          "stop_reason": "end_turn", "usage": {}})
        await AnthropicProvider(gw, "k").complete(
            model="claude-haiku-4-5", system="", messages=[Message.user("h")],
            tools=[], max_tokens=10, temperature=0.2,
        )
        self.assertEqual(gw.last["json"]["temperature"], 0.2)

    async def test_refusal_becomes_a_retryable_error(self):
        gw = StubGateway({
            "content": [], "stop_reason": "refusal",
            "stop_details": {"type": "refusal", "category": "cyber"},
        })
        with self.assertRaises(ProviderError) as ctx:
            await AnthropicProvider(gw, "k").complete(
                model="claude-opus-5", system="", messages=[Message.user("h")],
                tools=[], max_tokens=10,
            )
        self.assertTrue(ctx.exception.retryable)

    async def test_route_options_reach_the_wire(self):
        gw = StubGateway({"content": [{"type": "text", "text": "x"}],
                          "stop_reason": "end_turn", "usage": {}})
        p = AnthropicProvider(gw, "k", options={"thinking": "adaptive", "effort": "low"})
        await p.complete(model="claude-opus-5", system="", messages=[Message.user("h")],
                         tools=[], max_tokens=10)
        self.assertEqual(gw.last["json"]["thinking"], {"type": "adaptive"})
        self.assertEqual(gw.last["json"]["output_config"], {"effort": "low"})

    async def test_missing_key_is_not_retryable(self):
        with self.assertRaises(ProviderError) as ctx:
            await AnthropicProvider(StubGateway(), "").complete(
                model="m", system="", messages=[], tools=[], max_tokens=1)
        self.assertFalse(ctx.exception.retryable)


class TestOpenAIAdapter(unittest.IsolatedAsyncioTestCase):
    async def test_tool_results_become_role_tool_messages(self):
        gw = StubGateway({
            "choices": [{"message": {"content": "done", "tool_calls": None},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        })
        p = OpenAICompatProvider(gw, "sk-x", slug="openai")
        await p.complete(
            model="gpt-4o", system="sys",
            messages=[
                Message.user("q"),
                Message(role="assistant", content=[ToolUse("c1", "f", {"a": 1})]),
                Message(role="user", content=[ToolResult("c1", "42")]),
            ],
            tools=[ToolSpec("f", "d", {"type": "object", "properties": {}})],
            max_tokens=50,
        )
        wire = gw.last["json"]["messages"]
        self.assertEqual(wire[0]["role"], "system")
        # The assistant turn carries tool_calls with JSON-string arguments.
        assistant = wire[2]
        self.assertEqual(assistant["tool_calls"][0]["function"]["arguments"], '{"a": 1}')
        # The result becomes its own role:"tool" message keyed by call id.
        self.assertEqual(wire[3]["role"], "tool")
        self.assertEqual(wire[3]["tool_call_id"], "c1")
        # OpenAI nests the schema under function.parameters.
        self.assertIn("parameters", gw.last["json"]["tools"][0]["function"])

    async def test_parses_tool_calls_and_bad_json_arguments(self):
        gw = StubGateway({
            "choices": [{
                "message": {"content": None, "tool_calls": [
                    {"id": "c1", "function": {"name": "f", "arguments": '{"a": 1}'}},
                    {"id": "c2", "function": {"name": "g", "arguments": "not json"}},
                ]},
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        })
        out = await OpenAICompatProvider(gw, "k").complete(
            model="m", system="", messages=[Message.user("x")], tools=[], max_tokens=10)
        self.assertEqual(out.tool_calls[0].input, {"a": 1})
        # Malformed arguments must not crash the turn.
        self.assertEqual(out.tool_calls[1].input, {})


class TestGoogleAdapter(unittest.IsolatedAsyncioTestCase):
    def test_schema_cleaning_strips_unsupported_keywords(self):
        cleaned = _clean_schema({
            "type": "object",
            "additionalProperties": False,
            "$schema": "http://json-schema.org/draft-07/schema#",
            "properties": {"a": {"type": "string", "default": "", "description": "d"}},
            "required": ["a"],
        })
        self.assertNotIn("additionalProperties", cleaned)
        self.assertNotIn("$schema", cleaned)
        self.assertNotIn("default", cleaned["properties"]["a"])
        self.assertEqual(cleaned["properties"]["a"]["description"], "d")
        self.assertEqual(cleaned["required"], ["a"])

    async def test_roles_and_function_calls(self):
        gw = StubGateway({
            "candidates": [{
                "content": {"parts": [
                    {"text": "sure"},
                    {"functionCall": {"name": "f", "args": {"a": 1}}},
                ]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
        })
        p = GoogleProvider(gw, "AIza-test")
        out = await p.complete(
            model="gemini-2.0-flash", system="sys",
            messages=[Message.user("q"), Message.assistant("a")],
            tools=[ToolSpec("f", "d", {"type": "object", "properties": {}})],
            max_tokens=64,
        )
        body = gw.last["json"]
        self.assertEqual(gw.last["path"], "v1beta/models/gemini-2.0-flash:generateContent")
        self.assertEqual(gw.last["headers"]["x-goog-api-key"], "AIza-test")
        self.assertEqual(body["system_instruction"]["parts"][0]["text"], "sys")
        # Gemini calls the assistant role "model".
        self.assertEqual(body["contents"][1]["role"], "model")
        self.assertIn("function_declarations", body["tools"][0])
        self.assertEqual(out.text, "sure")
        self.assertEqual(out.tool_calls[0].name, "f")

    async def test_tool_result_round_trips_by_name(self):
        gw = StubGateway({"candidates": [{"content": {"parts": [{"text": "ok"}]},
                                          "finishReason": "STOP"}]})
        await GoogleProvider(gw, "k").complete(
            model="m", system="",
            messages=[
                Message.user("q"),
                Message(role="assistant", content=[ToolUse("myfunc:0", "myfunc", {})]),
                Message(role="user", content=[ToolResult("myfunc:0", "42")]),
            ],
            tools=[], max_tokens=10,
        )
        parts = gw.last["json"]["contents"][2]["parts"]
        # Gemini matches results to calls by name, so the id must decode back.
        self.assertEqual(parts[0]["functionResponse"]["name"], "myfunc")


class TestToolRegistry(unittest.IsolatedAsyncioTestCase):
    async def test_bad_arguments_are_reported_not_raised(self):
        reg = ToolRegistry()

        async def needs_id(entity_id: str):
            return "ok"

        reg.add("t", "d", obj({"entity_id": string("id")}, ["entity_id"]), needs_id)
        out, err = await reg.invoke("t", {"wrong_arg": 1})
        self.assertTrue(err)
        self.assertIn("Invalid arguments", out)

    async def test_sync_handlers_work_too(self):
        reg = ToolRegistry()
        reg.add("t", "d", obj({}), lambda: "sync result")
        out, err = await reg.invoke("t", {})
        self.assertFalse(err)
        self.assertEqual(out, "sync result")


if __name__ == "__main__":
    unittest.main(verbosity=2)
