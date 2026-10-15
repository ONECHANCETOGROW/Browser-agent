"""Phase 2.1 voice input: runs the Node unit tests for the side panel's STT controller and voice UI (tests/js/*.test.cjs) as part of `unittest discover`.

The voice code is plain browser JavaScript (extension/sidepanel/stt.js, voice_ui.js). Node is only the test runner here, exactly as it already is for the
real-Chrome launcher; nothing in the product depends on it.
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

JS_DIR = Path(__file__).resolve().parent / "js"


@unittest.skipUnless(shutil.which("node"), "Node.js is needed to run the voice unit tests")
class VoiceJsTests(unittest.TestCase):
    def test_voice_unit_tests_pass(self):
        files = sorted(str(p) for p in JS_DIR.glob("*.test.cjs"))
        self.assertTrue(files, "no voice tests found")
        r = subprocess.run(["node", "--test", "--test-reporter=spec", *files], capture_output=True, text=True, encoding="utf-8", timeout=120)
        out = r.stdout + r.stderr
        passed = int(re.search(r"ℹ pass (\d+)", out).group(1))
        failed = int(re.search(r"ℹ fail (\d+)", out).group(1))
        self.assertEqual(failed, 0, out[-4000:])
        self.assertGreaterEqual(passed, 40, "expected the full voice test set to run")
        self.assertEqual(r.returncode, 0, out[-2000:])


if __name__ == "__main__":
    unittest.main()
