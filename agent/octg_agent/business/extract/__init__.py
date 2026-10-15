"""Safe text extraction (runs a stdlib-only worker in a separate process) and deterministic field extraction. No model is involved."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

WORKER = Path(__file__).with_name("worker.py")
MAX_OUTPUT = 2_000_000


@dataclass
class ExtractResult:
    ok: bool
    text: str = ""
    extractor: str = ""
    note: str = ""


def extract_text(path: Path | str, ext: str, timeout_s: float = 20.0) -> ExtractResult:
    env = {k: v for k, v in os.environ.items() if k in ("SYSTEMROOT", "TEMP", "TMP", "PATH", "LANG")}  # no keys, tokens or proxies reach the worker
    try:
        p = subprocess.run([sys.executable, "-I", str(WORKER), str(path), ext], capture_output=True, timeout=timeout_s, env=env, stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        return ExtractResult(False, note="extraction timed out and was stopped")
    except OSError as e:
        return ExtractResult(False, note=f"extractor could not start: {type(e).__name__}")
    if len(p.stdout) > MAX_OUTPUT:
        return ExtractResult(False, note="extraction output too large")
    try:
        d = json.loads(p.stdout.decode("utf-8", "replace"))
    except ValueError:
        return ExtractResult(False, note="extractor failed")
    return ExtractResult(bool(d.get("ok")), d.get("text", ""), d.get("extractor", ""), d.get("note", ""))
