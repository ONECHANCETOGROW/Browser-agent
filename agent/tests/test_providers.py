"""Provider adapters, tested without any real model: fake HTTP servers for OpenAI-compatible / FreeLLMAPI / Ollama and a fake Gemini SDK client."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from octg_agent.reasoning.keys import KeyStore, redact
from octg_agent.reasoning.provider import Message, ProviderError, extract_json_object, tool_call_from_text
from octg_agent.reasoning.providers import build_providers
from octg_agent.reasoning.providers.freellmapi import FreeLLMAPIProvider
from octg_agent.reasoning.providers.gemini import GeminiProvider
from octg_agent.reasoning.providers.mock import MockProvider
from octg_agent.reasoning.providers.ollama import OllamaProvider
from octg_agent.reasoning.providers import openai_compat as oc
from octg_agent.reasoning.providers.openai_compat import OpenAICompatProvider

TOOLS = [{"name": "click", "description": "Click it.", "input_schema": {"type": "object", "properties": {"element_id": {"type": "string"}, "n": {"type": ["integer", "null"]}}, "required": ["element_id"],
                                                                         "additionalProperties": False}},
         {"name": "finish_task", "description": "End.", "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}]
MSGS = [Message("system", "You are an agent."), Message("user", "Click el_001.")]
SECRET = "sk-" "TESTSECRET0123456789abcdef"


class FakeServer:
    """A scriptable HTTP server. `responder(request_json, headers)` returns (status, body_dict, headers)."""

    def __init__(self, responder, path="/v1/chat/completions", extra_routes=None):
        self.responder, self.path, self.requests = responder, path, []
        self.app = web.Application()
        self.app.router.add_post(path, self._handle)
        for method, p, fn in extra_routes or []:
            self.app.router.add_route(method, p, fn)
        self.server = TestServer(self.app, host="127.0.0.1")

    async def _handle(self, request):
        body = await request.json()
        self.requests.append({"json": body, "headers": dict(request.headers)})
        status, data, headers = self.responder(body, request.headers)
        return web.json_response(data, status=status, headers=headers or {})

    async def __aenter__(self):
        await self.server.start_server()
        return self

    async def __aexit__(self, *a):
        await self.server.close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.port}"


def openai_reply(name="click", args=None, usage=(50, 9), text=None):
    msg = {"role": "assistant", "content": text}
    if name:
        msg["tool_calls"] = [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args or {"element_id": "el_001"})}}]
    return 200, {"choices": [{"message": msg}], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}, None


class JsonRescueTests(unittest.TestCase):
    def test_extract_and_rescue(self):
        self.assertEqual(extract_json_object('blah ```json\n{"a": {"b": 1}}\n``` x'), {"a": {"b": 1}})
        self.assertEqual(extract_json_object('<think>{"x":1}</think>{"y":2}'), {"y": 2})
        self.assertIsNone(extract_json_object("no json here"))
        tc = tool_call_from_text('Sure: {"tool": "click", "arguments": {"element_id": "el_3"}, "reason": "go"}')
        self.assertEqual((tc.name, tc.arguments["element_id"], tc.arguments["reason"]), ("click", "el_3", "go"))
        self.assertEqual(tool_call_from_text('{"name": "finish_task", "args": "{\\"summary\\": \\"x\\"}"}').arguments, {"summary": "x"})
        self.assertIsNone(tool_call_from_text('{"nothing": 1}'))


class OpenAICompatTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_tool_call_usage_and_key_header(self):
        async with FakeServer(lambda b, h: openai_reply()) as s:
            p = OpenAICompatProvider(s.url + "/v1", "m", lambda: SECRET, name="x")
            r = await p.complete(MSGS, TOOLS)
            await p.close()
            self.assertEqual((r.tool_calls[0].name, r.tool_calls[0].arguments), ("click", {"element_id": "el_001"}))
            self.assertEqual(r.usage, {"input_tokens": 50, "output_tokens": 9})
            req = s.requests[0]
            self.assertEqual(req["headers"]["Authorization"], f"Bearer {SECRET}")
            self.assertEqual(req["json"]["tool_choice"], "required")
            self.assertEqual(req["json"]["tools"][0]["function"]["name"], "click")
            self.assertEqual(req["json"]["messages"][0]["role"], "system")
            self.assertEqual((r.provider, r.model), ("x", "m"))

    async def test_tool_choice_rejected_retries_with_auto(self):
        def responder(b, h):
            return (400, {"error": "tool_choice 'required' is not supported"}, None) if b.get("tool_choice") == "required" else openai_reply()
        async with FakeServer(responder) as s:
            p = OpenAICompatProvider(s.url + "/v1", "m")
            r = await p.complete(MSGS, TOOLS)
            await p.close()
            self.assertEqual(r.tool_calls[0].name, "click")
            self.assertEqual([q["json"].get("tool_choice") for q in s.requests], ["required", "auto"])

    async def test_server_without_tool_support_falls_back_to_json_mode(self):
        def responder(b, h):
            if b.get("tools"):
                return 400, {"error": "this model does not support tools"}, None
            assert "ONE JSON object" in b["messages"][0]["content"]
            return openai_reply(None, text='{"tool": "finish_task", "arguments": {"summary": "ok"}}')
        async with FakeServer(responder) as s:
            p = OpenAICompatProvider(s.url + "/v1", "m")
            r = await p.complete(MSGS, TOOLS)
            await p.close()
            self.assertEqual((r.tool_calls[0].name, r.tool_calls[0].arguments), ("finish_task", {"summary": "ok"}))
            self.assertFalse(p.capabilities.native_tools)

    async def test_plain_text_tool_call_is_rescued(self):
        async with FakeServer(lambda b, h: openai_reply(None, text='I will click. {"name": "click", "arguments": {"element_id": "el_9"}}')) as s:
            p = OpenAICompatProvider(s.url + "/v1", "m")
            r = await p.complete(MSGS, TOOLS)
            await p.close()
            self.assertEqual(r.tool_calls[0].arguments["element_id"], "el_9")

    async def test_http_errors_map_to_provider_error_kinds_without_leaking_the_key(self):
        cases = [(429, "rate_limit"), (401, "auth"), (503, "unavailable"), (400, "bad_request")]
        for status, kind in cases:
            async with FakeServer(lambda b, h, st=status: (st, {"error": f"boom for key {SECRET}"}, {"Retry-After": "7"} if st == 429 else None)) as s:
                p = OpenAICompatProvider(s.url + "/v1", "m", lambda: SECRET)
                with self.assertRaises(ProviderError) as cm:
                    await p.complete(MSGS, TOOLS)
                await p.close()
                self.assertEqual(cm.exception.kind, kind)
                self.assertNotIn(SECRET, str(cm.exception))
                if status == 429:
                    self.assertEqual(cm.exception.retry_after, 7.0)

    async def test_unreachable_endpoint_is_unavailable(self):
        p = OpenAICompatProvider("http://127.0.0.1:9/v1", "m", timeout_s=2)
        ok, why = await p.available()
        self.assertFalse(ok)
        with self.assertRaises(ProviderError) as cm:
            await p.complete(MSGS, TOOLS)
        await p.close()
        self.assertIn(cm.exception.kind, ("unavailable", "timeout"))  # Windows may time out instead of refusing a dead loopback port

    async def test_timeout_is_reported(self):
        async def slow(request):
            await asyncio.sleep(3)
            return web.json_response({})
        app = web.Application()
        app.router.add_post("/v1/chat/completions", slow)
        srv = TestServer(app, host="127.0.0.1")
        await srv.start_server()
        try:
            p = OpenAICompatProvider(f"http://127.0.0.1:{srv.port}/v1", "m", timeout_s=0.3)
            with self.assertRaises(ProviderError) as cm:
                await p.complete(MSGS, TOOLS)
            await p.close()
            self.assertEqual(cm.exception.kind, "timeout")
        finally:
            await srv.close()


class FreeLLMAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_and_optional_nature(self):
        p = FreeLLMAPIProvider(key_getter=lambda: None)
        self.assertEqual((p.base_url, p.model, p.name), ("http://localhost:3001/v1", "auto", "freellmapi"))
        ok, why = await p.available()
        self.assertFalse(ok)
        self.assertIn("no FreeLLMAPI key", why)
        await p.close()

    async def test_works_through_its_openai_compatible_endpoint_with_a_bearer_key(self):
        async def models(request):
            return web.json_response({"data": [{"id": "auto"}]}) if request.headers.get("Authorization") == "Bearer freellmapi-abc12345" else web.Response(status=401)
        async with FakeServer(lambda b, h: openai_reply("finish_task", {"summary": "ok"}), extra_routes=[("GET", "/v1/models", models)]) as s:
            p = FreeLLMAPIProvider(s.url + "/v1", "auto:fast", lambda: "freellmapi-abc12345")
            self.assertEqual((await p.available())[0], True)
            r = await p.complete(MSGS, TOOLS)
            await p.close()
            self.assertEqual(r.tool_calls[0].name, "finish_task")
            self.assertEqual(s.requests[0]["json"]["model"], "auto:fast")
            self.assertEqual(r.provider, "freellmapi")


PARSE_400 = {"error": {"message": "All routed providers rejected the request as invalid. Groq API error 400: Parsing failed. The model "
                       "generated output that could not be parsed. See 'failed_generation' for the tool call"}}


class FreeLLMAPIRetryTests(unittest.IsolatedAsyncioTestCase):
    """One retry on transient upstream failures, FreeLLMAPI only, never for our own bad requests."""

    def setUp(self):
        self.sleeps = []

        async def fake_sleep(seconds):
            self.sleeps.append(seconds)
        self._patch = patch.object(oc, "_sleep", fake_sleep)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    @staticmethod
    def scripted(*replies):
        it = iter(replies)
        return lambda b, h: next(it)

    async def run_free(self, responder, provider_cls=FreeLLMAPIProvider):
        async with FakeServer(responder) as s:
            p = provider_cls(s.url + "/v1", "auto", lambda: SECRET) if provider_cls is FreeLLMAPIProvider else provider_cls(s.url + "/v1", "m", lambda: SECRET)
            try:
                try:
                    return s, p, await p.complete(MSGS, TOOLS), None
                except ProviderError as e:
                    return s, p, None, e
            finally:
                await p.close()

    async def test_success_is_never_retried(self):
        s, _, r, err = await self.run_free(lambda b, h: openai_reply())
        self.assertIsNone(err)
        self.assertEqual(len(s.requests), 1)
        self.assertEqual(self.sleeps, [])

    async def test_502_is_retried_once_with_the_identical_request(self):
        s, p, r, err = await self.run_free(self.scripted((502, {"error": "bad gateway"}, None), openai_reply()))
        self.assertIsNone(err)
        self.assertEqual(r.tool_calls[0].name, "click")
        self.assertEqual(len(s.requests), 2)
        self.assertEqual(s.requests[0]["json"], s.requests[1]["json"])           # same messages/tools; tool_choice stays "required"
        self.assertEqual(s.requests[1]["json"]["tool_choice"], "required")
        self.assertEqual(self.sleeps, [oc._RETRY_DELAY_S])

    async def test_parse_failure_400_is_retried_and_does_not_flip_the_provider_to_json_mode(self):
        s, p, r, err = await self.run_free(self.scripted((400, PARSE_400, None), openai_reply()))
        self.assertIsNone(err)
        self.assertEqual(len(s.requests), 2)
        self.assertTrue(p.capabilities.native_tools)                              # body mentions "tool", still native
        self.assertFalse(p._json_fallback)
        self.assertEqual([q["json"]["tool_choice"] for q in s.requests], ["required", "required"])

    async def test_429_honours_a_short_retry_after_and_caps_a_long_one(self):
        s, _, r, err = await self.run_free(self.scripted((429, {"error": "slow down"}, {"Retry-After": "3"}), openai_reply()))
        self.assertIsNone(err)
        self.assertEqual(self.sleeps, [3.0])
        self.sleeps.clear()
        s, _, r, err = await self.run_free(self.scripted((429, {"error": "slow down"}, {"Retry-After": "60"}), openai_reply()))
        self.assertIsNone(err)
        self.assertEqual(self.sleeps, [oc._RETRY_AFTER_CAP_S])

    async def test_only_one_retry_then_the_original_error_kind_surfaces(self):
        for status, kind in ((502, "unavailable"), (429, "rate_limit"), (400, "bad_request")):
            body = PARSE_400 if status == 400 else {"error": "x"}
            s, _, r, err = await self.run_free(lambda b, h, st=status, bd=body: (st, bd, None))
            self.assertEqual(len(s.requests), 2, status)
            self.assertEqual(err.kind, kind, status)

    async def test_other_client_errors_are_not_retried(self):
        for status, body in ((400, {"error": "invalid request: messages must not be empty"}), (401, {"error": "bad key"}),
                             (403, {"error": "forbidden"}), (404, {"error": "no such model"}), (422, {"error": "bad schema"})):
            s, _, r, err = await self.run_free(lambda b, h, st=status, bd=body: (st, bd, None))
            self.assertEqual(len(s.requests), 1, status)
            self.assertIsNotNone(err)
        self.assertEqual(self.sleeps, [])

    async def test_generic_openai_compatible_endpoints_do_not_retry(self):
        s, _, r, err = await self.run_free(lambda b, h: (502, {"error": "x"}, None), provider_cls=OpenAICompatProvider)
        self.assertEqual(len(s.requests), 1)
        self.assertEqual(err.kind, "unavailable")

    async def test_retry_does_not_weaken_the_existing_tool_negotiation(self):
        # transient 502 first, then the endpoint rejects tool_choice=required -> still falls back to auto exactly once
        def responder(b, h, n=[0]):
            n[0] += 1
            if n[0] == 1:
                return 502, {"error": "x"}, None
            return (400, {"error": "tool_choice 'required' is not supported"}, None) if b.get("tool_choice") == "required" else openai_reply()
        s, _, r, err = await self.run_free(responder)
        self.assertIsNone(err)
        self.assertEqual([q["json"].get("tool_choice") for q in s.requests], ["required", "required", "auto"])

    async def test_retry_log_names_the_reason_but_never_content_or_keys(self):
        with self.assertLogs("octg.provider", level="WARNING") as cm:
            await self.run_free(self.scripted((400, PARSE_400, None), openai_reply()))
        text = "\n".join(cm.output)
        self.assertIn("upstream_tool_call_parse_failure", text)
        self.assertIn("retrying once", text)
        for secret in (SECRET, "Click el_001.", "You are an agent.", "failed_generation for the tool call"):
            self.assertNotIn(secret, text)

    def test_transient_reason_classifier(self):
        self.assertEqual(oc.transient_reason(502, ""), "http_502")
        self.assertEqual(oc.transient_reason(429, ""), "http_429")
        self.assertEqual(oc.transient_reason(400, "Groq API error 400: Parsing failed."), "upstream_tool_call_parse_failure")
        self.assertIsNone(oc.transient_reason(400, "messages must not be empty"))
        self.assertIsNone(oc.transient_reason(401, ""))
        self.assertIsNone(oc.transient_reason(200, ""))


class OllamaTests(unittest.IsolatedAsyncioTestCase):
    def ollama(self, responder, models=("qwen3:8b",)):
        async def tags(request):
            return web.json_response({"models": [{"name": m} for m in models]})
        return FakeServer(responder, "/api/chat", [("GET", "/api/tags", tags)])

    async def test_native_call_with_thinking_off_and_exact_token_counts(self):
        def responder(b, h):
            return 200, {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "click", "arguments": {"element_id": "el_002"}}}]},
                         "prompt_eval_count": 321, "eval_count": 17}, None
        async with self.ollama(responder) as s:
            p = OllamaProvider(s.url, "qwen3:8b", num_ctx=4096)
            self.assertEqual((await p.available())[0], True)
            r = await p.complete(MSGS, TOOLS, max_output_tokens=123)
            await p.close()
            self.assertEqual(r.tool_calls[0].arguments, {"element_id": "el_002"})
            self.assertEqual(r.usage, {"input_tokens": 321, "output_tokens": 17})
            j = s.requests[0]["json"]
            self.assertIs(j["think"], False)
            self.assertEqual(j["options"]["num_ctx"], 4096)
            self.assertEqual(j["options"]["num_predict"], 123)
            self.assertIs(j["stream"], False)
            self.assertTrue(p.capabilities.local)

    async def test_model_without_tool_support_uses_json_mode(self):
        def responder(b, h):
            if b.get("tools"):
                return 400, {"error": "registry.ollama.ai/library/x does not support tools"}, None
            return 200, {"message": {"content": '<think>hmm</think>{"tool": "click", "arguments": {"element_id": "el_1"}}'}, "prompt_eval_count": 5, "eval_count": 5}, None
        async with self.ollama(responder) as s:
            p = OllamaProvider(s.url, "qwen3:8b")
            r = await p.complete(MSGS, TOOLS)
            await p.close()
            self.assertEqual(r.tool_calls[0].name, "click")

    async def test_availability_explains_what_is_missing(self):
        async with self.ollama(lambda b, h: (200, {}, None), models=("gemma4:latest",)) as s:
            ok, why = await OllamaProvider(s.url, "qwen3:8b").available()
            self.assertFalse(ok)
            self.assertIn("not installed", why)
            self.assertIn("gemma4:latest", why)
            self.assertTrue((await OllamaProvider(s.url, "gemma4").available())[0])
        ok, why = await OllamaProvider("http://127.0.0.1:9", "qwen3:8b").available()
        self.assertFalse(ok)
        self.assertTrue("ollama" in why.lower())

    async def test_unreachable_runtime_is_unavailable_and_never_installs_anything(self):
        p = OllamaProvider("http://127.0.0.1:9", "qwen3:8b", timeout_s=2)
        with self.assertRaises(ProviderError) as cm:
            await p.complete(MSGS, TOOLS)
        await p.close()
        self.assertIn(cm.exception.kind, ("unavailable", "timeout"))
        if cm.exception.kind == "unavailable":
            self.assertIn("ollama serve", str(cm.exception))


class FakeGeminiClient:
    def __init__(self, behaviour):
        self.calls = []
        client = self

        class Models:
            async def generate_content(self, model, contents, config):
                client.calls.append({"model": model, "contents": contents, "config": config})
                return behaviour(model, contents, config)
        self.aio = SimpleNamespace(models=Models())


def gemini_reply(name="click", args=None, usage=(111, 22, 5)):
    part = SimpleNamespace(function_call=SimpleNamespace(name=name, args=args or {"element_id": "el_001"}, id=None), text=None, thought=False)
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))],
                           usage_metadata=SimpleNamespace(prompt_token_count=usage[0], candidates_token_count=usage[1], thoughts_token_count=usage[2]))


class FakeApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class GeminiTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, behaviour, key=SECRET):
        fake = FakeGeminiClient(behaviour)
        return GeminiProvider("gemini-3.5-flash-lite", lambda: key, client_factory=lambda k: fake), fake

    async def test_request_uses_current_sdk_shape_and_forces_a_tool_call(self):
        p, fake = self.provider(lambda *a: gemini_reply())
        r = await p.complete([Message("system", "SYS"), Message("user", "do it", images=[b"\x89PNG\r\n\x1a\n0000"])], TOOLS, force_tool=True, max_output_tokens=200)
        call = fake.calls[0]
        cfg = call["config"]
        self.assertEqual(call["model"], "gemini-3.5-flash-lite")
        self.assertEqual(cfg.system_instruction, "SYS")
        decls = cfg.tools[0].function_declarations
        self.assertEqual([d.name for d in decls], ["click", "finish_task"])
        schema = decls[0].parameters_json_schema
        self.assertNotIn("additionalProperties", schema)
        self.assertEqual(schema["properties"]["n"]["type"], "integer")  # ["integer","null"] collapsed to something Gemini accepts
        self.assertTrue(cfg.automatic_function_calling.disable, "the SDK must not run tools itself; the dispatcher does")
        self.assertEqual(str(cfg.tool_config.function_calling_config.mode.value if hasattr(cfg.tool_config.function_calling_config.mode, "value") else cfg.tool_config.function_calling_config.mode).upper(), "ANY")
        parts = call["contents"][0].parts
        self.assertEqual(len(parts), 2)  # text + inline image
        self.assertEqual(cfg.max_output_tokens, 200)
        self.assertEqual((r.tool_calls[0].name, r.tool_calls[0].arguments), ("click", {"element_id": "el_001"}))
        self.assertEqual(r.usage, {"input_tokens": 111, "output_tokens": 27})  # candidates + thinking tokens

    async def test_missing_key_is_not_configured_and_never_calls_out(self):
        p, fake = self.provider(lambda *a: gemini_reply(), key=None)
        self.assertEqual((await p.available())[0], False)
        with self.assertRaises(ProviderError) as cm:
            await p.complete(MSGS, TOOLS)
        self.assertEqual(cm.exception.kind, "not_configured")
        self.assertEqual(fake.calls, [])

    async def test_api_errors_are_mapped_and_redacted(self):
        for code, msg, kind in [(429, "RESOURCE_EXHAUSTED quota; retry in 12s", "rate_limit"), (403, "PERMISSION_DENIED", "auth"), (400, f"API key not valid {SECRET}", "auth"),
                                (400, "INVALID_ARGUMENT bad schema", "bad_request"), (503, "UNAVAILABLE overloaded", "unavailable")]:
            def boom(*a, code=code, msg=msg):
                raise FakeApiError(code, msg + f" key={SECRET}")
            p, _ = self.provider(boom)
            with self.assertRaises(ProviderError) as cm:
                await p.complete(MSGS, TOOLS)
            self.assertEqual(cm.exception.kind, kind, (code, msg))
            self.assertNotIn(SECRET, str(cm.exception))
            if code == 429:
                self.assertEqual(cm.exception.retry_after, 12.0)

    async def test_timeout_and_empty_response(self):
        async def hang(*a):
            await asyncio.sleep(5)
        fake = FakeGeminiClient(lambda *a: None)

        class Slow:
            async def generate_content(self, **kw):
                await asyncio.sleep(5)
        fake.aio = SimpleNamespace(models=Slow())
        p = GeminiProvider("m", lambda: "k", timeout_s=0.2, client_factory=lambda k: fake)
        with self.assertRaises(ProviderError) as cm:
            await p.complete(MSGS, TOOLS)
        self.assertEqual(cm.exception.kind, "timeout")
        p, _ = self.provider(lambda *a: SimpleNamespace(candidates=[], usage_metadata=None))
        with self.assertRaises(ProviderError) as cm:
            await p.complete(MSGS, TOOLS)
        self.assertEqual(cm.exception.kind, "invalid_response")

    async def test_real_sdk_objects_accept_our_declarations(self):
        """Guards against SDK API drift: builds the real google-genai types without calling the network."""
        p = GeminiProvider("gemini-3.5-flash-lite", lambda: "k")
        contents, config = p._build(MSGS, TOOLS, {})
        self.assertEqual(config.tools[0].function_declarations[1].name, "finish_task")


class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="octg_keys_"))
        self._saved = {k: os.environ.pop(k, None) for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "FREELLMAPI_API_KEY")}

    def tearDown(self):
        import shutil
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_wins_then_file_and_describe_hides_the_value(self):
        ks = KeyStore(self.tmp)
        self.assertIsNone(ks.get("gemini"))
        ks.set("gemini", "file-key-123456")
        self.assertEqual(ks.get("gemini"), "file-key-123456")
        os.environ["GEMINI_API_KEY"] = "env-key-123456"
        try:
            self.assertEqual(ks.get("gemini"), "env-key-123456")
            d = ks.describe("gemini")
            self.assertEqual(d, {"key_configured": True, "key_source": "env:GEMINI_API_KEY"})
            self.assertNotIn("123456", json.dumps(d))
        finally:
            del os.environ["GEMINI_API_KEY"]
        self.assertTrue(ks.remove("gemini"))
        self.assertFalse(ks.describe("gemini")["key_configured"])

    def test_redact_scrubs_known_and_shaped_keys(self):
        self.assertNotIn("abcdef123456", redact("failed with abcdef123456", "abcdef123456"))
        self.assertNotIn("AIza" "SyA1234567890123456789012345", redact("key=AIza" "SyA1234567890123456789012345"))
        self.assertNotIn("freellmapi-abcdefgh1234", redact("freellmapi-abcdefgh1234"))

    def test_factory_builds_all_providers_without_network_or_secrets(self):
        provs = build_providers({}, KeyStore(self.tmp))
        self.assertEqual(set(provs), {"local", "gemini", "freellmapi"})
        for p in provs.values():
            self.assertNotIn("key", json.dumps(p.describe()).lower().replace("key_", ""))
        provs = build_providers({"local": {"runtime": "openai", "url": "http://127.0.0.1:1234/v1", "model": "x"}, "freellmapi": {"enabled": False}}, KeyStore(self.tmp))
        self.assertEqual(set(provs), {"local", "gemini"})
        self.assertEqual(provs["local"].kind, "local")


if __name__ == "__main__":
    unittest.main()
