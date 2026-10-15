"""Phase 2.1 LIVE voice test (opt-in, not part of the regression suites).

Real installed Chrome (fresh temporary profile) + the real side panel + Chrome's REAL on-device speech recogniser + the REAL local model (Ollama) through the
normal task pipeline. The microphone is Chrome's fake audio device playing a synthetic-voice WAV of the sentence (a human voice is not available to a script),
so this checks the real engine/pipeline end to end but says nothing about real-room noise.

  python -m tests.e2e.make_test_speech "Open Google and search for One Chance to Grow." speech.wav
  python -m tests.e2e.run_voice_live speech.wav "Open Google and search for One Chance to Grow."

Only the local provider is configured (no API keys, no FreeLLMAPI, no Gemini). Nothing is saved except the temporary profile, which is deleted.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import sys
import time
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from octg_agent.brain.settings import BrainSettings  # noqa: E402
from octg_agent.reasoning.keys import KeyStore  # noqa: E402
from octg_agent.reasoning.providers import build_providers  # noqa: E402
from tests.e2e.run_ai_e2e import AI, AIEnv, EXT_ID  # noqa: E402

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def cdp(target: dict, expression: str, gesture: bool = False):
    async def go():
        async with aiohttp.ClientSession() as s, s.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=0) as ws:
            await ws.send_json({"id": 1, "method": "Runtime.evaluate", "params": {"expression": expression, "awaitPromise": True, "returnByValue": True, "userGesture": gesture}})
            async for m in ws:
                d = json.loads(m.data)
                if d.get("id") == 1:
                    if "exceptionDetails" in d.get("result", {}):
                        raise RuntimeError(d["result"]["exceptionDetails"])
                    return d["result"]["result"].get("value")
    return asyncio.run(go())


def main() -> int:
    wav, expected = Path(sys.argv[1]).resolve(), sys.argv[2]
    speech_s, lead_s = float(os.environ.get("SPEECH_S", "3.75")), float(os.environ.get("LEAD_S", "2.0"))
    providers = {"local": build_providers({"local": {"timeout_s": 300}, "gemini": {"enabled": False}, "freellmapi": {"enabled": False}}, KeyStore(Path(".")))["local"]}
    brain = BrainSettings(max_steps=25, max_seconds=400, router={"policy": "LOCAL_FIRST"}, action_timeout_s=60)
    env = AIEnv(providers, brain, real_chrome=True,
                chrome_args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", f"--use-file-for-fake-audio-capture={wav}%noloop"])
    report: dict = {"expected": expected}
    try:
        env.start()
        t = AI(env)
        t.main = t.active()["tab_id"]
        report["browser"] = env.chrome_version
        panel = t.ok("new_tab", url=f"chrome-extension://{EXT_ID}/sidepanel/panel.html", active=False)["result"]["tab"]
        time.sleep(1.5)
        tgt = next(x for x in env.targets() if x["url"].endswith("sidepanel/panel.html") and x["type"] == "page")
        ev = lambda js, gesture=False: cdp(tgt, "(() => { const $ = (i) => document.getElementById(i); " + js + " })()", gesture)
        S = lambda: json.loads(ev("return JSON.stringify({mic: $('mic').dataset.state, text: $('taskInput').value, status: $('voiceStatus').textContent, installHidden: $('voiceInstall').hidden, "
                                  "badge: $('badge').textContent, runDisabled: $('run').disabled, taskStatus: $('status').textContent, task: $('task').textContent, info: window.octgVoice.controller.info});"))
        assert S()["badge"] == "connected", S()
        report["permission_state_before"] = ev("return navigator.permissions.query({name: 'microphone'}).then(r => r.state);")
        report["engine_availability_before"] = ev("const SR = window.SpeechRecognition || window.webkitSpeechRecognition; return SR.available({langs: ['en-US'], processLocally: true});")
        print("browser:", env.chrome_version, "| on-device speech model before:", report["engine_availability_before"], flush=True)

        # 1. the mic button shows that a one-time local model is needed, and nothing is downloaded until the user asks
        ev("$('mic').click(); return 1;")
        time.sleep(1.0)
        s = S()
        report["first_click"] = {"mic": s["mic"], "status": s["status"], "install_button_visible": not s["installHidden"]}
        print("first click ->", report["first_click"], flush=True)
        if report["engine_availability_before"] != "available":
            assert not s["installHidden"], s
            size0, t0 = dir_size(env.tmp / "profile"), time.time()
            ev("$('voiceInstall').click(); return 1;", gesture=True)  # a trusted click: installing needs user activation
            avail = None
            while time.time() - t0 < 600:
                avail = ev("const SR = window.SpeechRecognition || window.webkitSpeechRecognition; return SR.available({langs: ['en-US'], processLocally: true});")
                if avail == "available":
                    break
                time.sleep(2)
            report["install"] = {"result": avail, "seconds": round(time.time() - t0), "profile_growth_mb": round((dir_size(env.tmp / "profile") - size0) / 1e6, 1)}
            print("install ->", report["install"], flush=True)
            assert avail == "available", report["install"]
            time.sleep(1.0)

        # 2. a fresh blank tab is the active one; the panel (inactive tab) is the "side panel"
        fresh = t.ok("new_tab", url="about:blank", active=True)["result"]["tab"]
        report["fresh_tab"] = fresh["tab_id"]
        ev("$('taskInput').value = ''; return 1;")

        # 3. click the microphone, speak (the fake device plays the WAV), watch interim text, click again to stop
        timeline: list[tuple[float, str, str]] = []
        t_click = time.time()
        ev("$('mic').click(); return 1;")
        stop_at = t_click + lead_s + speech_s + 1.0
        stopped = False
        t_stop = None
        last = ("", "")
        while time.time() < stop_at + 20:
            s = S()
            now = round(time.time() - t_click, 2)
            if (s["mic"], s["text"]) != last:
                timeline.append((now, s["mic"], s["text"]))
                last = (s["mic"], s["text"])
            if not stopped and time.time() >= stop_at and s["mic"] == "listening":
                ev("$('mic').click(); return 1;")
                stopped, t_stop = True, time.time()
            if s["mic"] in ("idle", "error", "permission_denied", "unsupported") and (stopped or now > 2):
                break
            time.sleep(0.12)
        final = S()
        t_final = None
        for ts, mic, text in timeline:
            if mic in ("idle",) and text:
                t_final = ts
                break
        report["timeline"] = [{"t": a, "mic": b, "text": c} for a, b, c in timeline]
        report["listening_info"] = next((x for x in [final["info"]] if x), {})
        norm = lambda x: re.sub(r"[^a-z0-9 ]", "", x.lower()).split()
        transcript = final["text"]
        report["transcript"] = transcript
        report["word_similarity"] = round(difflib.SequenceMatcher(None, norm(expected), norm(transcript)).ratio(), 3)
        report["stop_clicked_at_s"] = round(t_stop - t_click, 2) if t_stop else None
        report["final_text_at_s"] = t_final
        report["transcription_latency_after_stop_s"] = round(t_final - (t_stop - t_click), 2) if (t_final and t_stop) else None
        report["interim_seen"] = any(m == "listening" and c and c != transcript for _, m, c in timeline)
        report["interim_values"] = [c for _, m, c in timeline if m == "listening" and c][:8]
        print("transcript:", repr(transcript), "| similarity", report["word_similarity"], flush=True)

        # 4. the existing pipeline takes it from here (auto-run -> Run button -> task engine)
        end = time.time() + 15
        started = None
        while time.time() < end:
            cur = t.current() if env.rt.tasks.current_id else None
            if cur and cur["user_instruction"].strip() == transcript.strip():
                started = cur
                break
            time.sleep(0.3)
        report["task_started"] = bool(started)
        if started:
            task = t.wait_status(TERMINAL, timeout=420)
            report["task"] = {k: task.get(k) for k in ("task_id", "status", "user_instruction", "result", "error", "step_count", "model_calls", "duration_s")}
            report["usage"] = task.get("usage")
            report["steps"] = [{"tool": s.get("tool"), "ok": s.get("ok"), "outcome": str(s.get("outcome"))[:100], "provider": s.get("provider")} for s in (task.get("completed_steps") or [])]
            report["final_url"] = next((x["url"] for x in t.ok("get_tabs")["result"]["tabs"] if x["active"]), None)
            report["all_urls"] = [x["url"] for x in t.ok("get_tabs")["result"]["tabs"]]
        print(json.dumps(report, indent=1)[:6000])
    except BaseException as e:  # noqa: BLE001 - the live run is expensive: always keep what was learned
        import traceback
        report["error"] = f"{type(e).__name__}: {e}"
        try:  # what the agent had done when the run ended (e.g. paused at a CAPTCHA)
            cur = t.current()
            report["task_at_end"] = {k: cur.get(k) for k in ("status", "user_instruction", "step_count", "model_calls", "waiting_message", "usage", "current_url")}
            report["steps_at_end"] = [{"tool": s.get("tool"), "ok": s.get("ok"), "provider": s.get("provider")} for s in (cur.get("completed_steps") or [])]
        except Exception:  # noqa: BLE001
            pass
        print("LIVE TEST ERROR:\n" + traceback.format_exc(limit=6), flush=True)
        print(json.dumps(report, indent=1, default=str)[:6000])
        return 1
    finally:
        env.stop()
        Path(__file__).with_name("last_voice_live.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
