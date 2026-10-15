import unittest

from octg_agent.reasoning.provider import Message, ProviderError
from octg_agent.reasoning.providers.mock import MockProvider
from octg_agent.reasoning.router import ModelRouter, NoProviderAvailable, Policy, RouteContext, RouterConfig

MSGS = [Message("system", "s"), Message("user", "u")]
TOOLS = [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
OK = MockProvider.call("t")


def provs(**kw):
    out = {}
    for name in ("local", "gemini", "freellmapi"):
        out[name] = kw.get(name) or MockProvider(handler=lambda m, t, o, n=name: OK, name=name, local=name == "local")
    return out


def simple(**kw):
    return RouteContext(step=kw.pop("step", 3), **kw)


COMPLEX = dict(first_step=True, consecutive_failures=1)


class RouterPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_first_sends_simple_steps_to_the_local_model(self):
        r = ModelRouter(provs(), RouterConfig(policy=Policy.LOCAL_FIRST))
        out = await r.complete(MSGS, TOOLS, simple())
        self.assertEqual(out.provider, "local")
        self.assertIn("simple", out.reason)

    async def test_local_first_escalates_complex_steps_to_a_stronger_model(self):
        p = provs()
        r = ModelRouter(p, RouterConfig(policy=Policy.LOCAL_FIRST))
        out = await r.complete(MSGS, TOOLS, simple(**COMPLEX))
        self.assertEqual(out.provider, "gemini")
        self.assertIn("complex", out.reason)
        self.assertEqual(len(p["local"].calls), 0)

    async def test_complexity_inputs_and_configurable_threshold(self):
        r = ModelRouter(provs(), RouterConfig())
        score, why = r.complexity(simple(first_step=True))
        self.assertEqual(score, 1.0)
        self.assertGreater(r.complexity(simple(needs_vision=True))[0], 2)
        self.assertGreater(r.complexity(simple(element_count=500, text_heavy=True, tool_errors=1))[0], 2)
        strict = ModelRouter(provs(), RouterConfig(complexity_threshold=0.5))
        self.assertEqual((await strict.complete(MSGS, TOOLS, simple(first_step=True))).provider, "gemini")
        lax = ModelRouter(provs(), RouterConfig(complexity_threshold=99))
        self.assertEqual((await lax.complete(MSGS, TOOLS, simple(**COMPLEX))).provider, "local")
        reweighted = ModelRouter(provs(), RouterConfig.from_dict({"weights": {"failure": 5}}))
        self.assertEqual((await reweighted.complete(MSGS, TOOLS, simple(consecutive_failures=1))).provider, "gemini")

    async def test_gemini_first_and_fallback_order(self):
        r = ModelRouter(provs(), RouterConfig(policy=Policy.GEMINI_FIRST))
        self.assertEqual((await r.complete(MSGS, TOOLS, simple())).provider, "gemini")
        r = ModelRouter(provs(), RouterConfig(policy=Policy.FALLBACK))
        self.assertEqual((await r.complete(MSGS, TOOLS, simple())).provider, "local")
        r = ModelRouter(provs(), RouterConfig.from_dict({"policy": "fallback", "order": ["freellmapi", "gemini"]}))
        self.assertEqual((await r.complete(MSGS, TOOLS, simple())).provider, "freellmapi")

    async def test_manual_uses_only_the_named_provider_with_no_fallback(self):
        p = provs(gemini=MockProvider(handler=lambda m, t, o: ProviderError("unavailable", "down"), name="gemini"))
        r = ModelRouter(p, RouterConfig(policy=Policy.MANUAL, manual_provider="gemini"))
        with self.assertRaises(NoProviderAvailable):
            await r.complete(MSGS, TOOLS, simple())
        self.assertEqual(len(p["local"].calls), 0)
        self.assertEqual(len(p["freellmapi"].calls), 0)


class RouterFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_providers_are_skipped(self):
        p = provs(local=MockProvider(handler=lambda *a: OK, name="local", local=True, up=False))
        out = await ModelRouter(p, RouterConfig(policy=Policy.FALLBACK)).complete(MSGS, TOOLS, simple())
        self.assertEqual(out.provider, "gemini")
        self.assertEqual(out.attempts[0]["error_kind"], "skipped")
        self.assertEqual(len(p["local"].calls), 0)

    async def test_failure_falls_through_local_gemini_freellmapi(self):
        boom = lambda n: MockProvider(handler=lambda *a: ProviderError("unavailable", "x"), name=n)
        p = provs(local=boom("local"), gemini=boom("gemini"))
        out = await ModelRouter(p, RouterConfig(policy=Policy.FALLBACK)).complete(MSGS, TOOLS, simple())
        self.assertEqual(out.provider, "freellmapi")
        self.assertEqual([a["provider"] for a in out.attempts], ["local", "gemini", "freellmapi"])
        self.assertIn("fell back to freellmapi", out.reason)

    async def test_rate_limit_puts_a_provider_on_cooldown_until_the_clock_passes(self):
        now = [100.0]
        p = provs(local=MockProvider([ProviderError("rate_limit", "429", retry_after=30)], name="local", local=True))
        r = ModelRouter(p, RouterConfig(policy=Policy.FALLBACK), clock=lambda: now[0])
        self.assertEqual((await r.complete(MSGS, TOOLS, simple())).provider, "gemini")
        self.assertEqual((await r.complete(MSGS, TOOLS, simple())).provider, "gemini")
        self.assertEqual(len(p["local"].calls), 1)  # not asked again while cooling down
        now[0] += 31
        p["local"].script = []
        p["local"].handler = lambda *a: OK
        self.assertEqual((await r.complete(MSGS, TOOLS, simple())).provider, "local")

    async def test_timeouts_retry_same_provider_then_escalate_and_stay_escalated(self):
        flaky = MockProvider(handler=lambda *a: ProviderError("timeout", "slow"), name="local", local=True)
        p = provs(local=flaky)
        r = ModelRouter(p, RouterConfig(policy=Policy.LOCAL_FIRST, escalate_after_failures=2, sticky_steps=3))
        out = await r.complete(MSGS, TOOLS, simple(step=5))
        self.assertEqual(out.provider, "gemini")
        self.assertEqual(len(flaky.calls), 2)
        later = await r.complete(MSGS, TOOLS, simple(step=6))  # sticky escalation: simple step, but still the stronger model
        self.assertEqual(later.provider, "gemini")
        self.assertIn("sticky", later.reason)
        r.clear_cooldowns()
        flaky.handler = lambda *a: OK  # the local runtime recovered
        much_later = await r.complete(MSGS, TOOLS, simple(step=20))
        self.assertEqual(much_later.provider, "local")

    async def test_manual_choice_ignores_cooldowns_left_by_earlier_failures(self):
        p = provs(local=MockProvider([ProviderError("unavailable", "blip")], name="local", local=True))
        auto = ModelRouter(p, RouterConfig(policy=Policy.FALLBACK))
        await auto.complete(MSGS, TOOLS, simple())  # local fails once -> cooldown, gemini answers
        manual = ModelRouter(p, RouterConfig(policy=Policy.MANUAL, manual_provider="local"), cooldown=auto._cooldown)
        p["local"].handler = lambda *a: OK
        self.assertEqual((await manual.complete(MSGS, TOOLS, simple())).provider, "local")

    async def test_vision_need_prefers_a_vision_capable_provider(self):
        p = provs(gemini=MockProvider(handler=lambda *a: OK, name="gemini", vision=True))
        out = await ModelRouter(p, RouterConfig(policy=Policy.FALLBACK)).complete(MSGS, TOOLS, simple(needs_vision=True))
        self.assertEqual(out.provider, "gemini")

    async def test_everything_failing_reports_every_attempt(self):
        p = {n: MockProvider(handler=lambda *a, n=n: ProviderError("auth", "bad key", provider=n), name=n) for n in ("local", "gemini")}
        with self.assertRaises(NoProviderAvailable) as cm:
            await ModelRouter(p, RouterConfig(policy=Policy.FALLBACK)).complete(MSGS, TOOLS, simple())
        self.assertEqual([a["provider"] for a in cm.exception.attempts], ["local", "gemini"])
        self.assertIn("bad key", str(cm.exception))

    async def test_status_reports_cooldowns_without_secrets(self):
        p = provs(gemini=MockProvider(handler=lambda *a: ProviderError("rate_limit", "x", retry_after=50), name="gemini"))
        r = ModelRouter(p, RouterConfig(policy=Policy.GEMINI_FIRST))
        await r.complete(MSGS, TOOLS, simple())
        rows = {x["name"]: x for x in await r.status()}
        self.assertFalse(rows["gemini"]["available"])
        self.assertIn("cooling down", rows["gemini"]["detail"])
        self.assertTrue(rows["local"]["available"])

    def test_config_parsing(self):
        c = RouterConfig.from_dict({"policy": "gemini_first", "complexity_threshold": 4, "weights": {"vision": 9}})
        self.assertEqual((c.policy, c.complexity_threshold, c.weights["vision"], c.weights["failure"]), (Policy.GEMINI_FIRST, 4, 9, 1.5))
        with self.assertRaises(ValueError):
            RouterConfig.from_dict({"policy": "WHATEVER"})


if __name__ == "__main__":
    unittest.main()
