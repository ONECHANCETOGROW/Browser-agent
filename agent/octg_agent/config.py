"""Settings and local secrets. Everything lives on this machine; nothing is fetched from a server."""
from __future__ import annotations

import json
import os
import secrets as _secrets
from dataclasses import dataclass, field, fields
from pathlib import Path

# Stable ID derived from the public key pinned in extension/manifest.json.
DEFAULT_EXTENSION_ID = "kfofbldlpmoobgiafcbohiiebmfggecg"


def default_data_dir() -> Path:
    return Path(os.environ.get("OCTG_DATA_DIR") or Path(__file__).resolve().parents[1] / "data")


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = field(default_factory=default_data_dir)
    extension_id: str = DEFAULT_EXTENSION_ID  # "" accepts any chrome-extension:// origin (token still required)
    extra_allowed_origins: tuple[str, ...] = ()  # test harnesses only
    action_timeout_s: float = 30.0
    max_upload_bytes: int = 15 * 1024 * 1024
    max_events: int = 500
    log_level: str = "INFO"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def secrets_path(self) -> Path:
        return self.data_dir / "secrets.json"

    @property
    def folders_path(self) -> Path:
        return self.data_dir / "authorized_folders.json"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.logs_dir, self.screenshots_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def load_settings(config_path: str | os.PathLike | None = None) -> Settings:
    """config.json (next to the package, or OCTG_CONFIG) < environment variables."""
    s = Settings()
    path = Path(config_path or os.environ.get("OCTG_CONFIG") or Path(__file__).resolve().parents[1] / "config.json")
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(Settings)}
        for k, v in raw.items():
            if k in known:
                setattr(s, k, Path(v) if k == "data_dir" else v)
    env = os.environ
    if env.get("OCTG_HOST"):
        s.host = env["OCTG_HOST"]
    if env.get("OCTG_PORT"):
        s.port = int(env["OCTG_PORT"])
    if env.get("OCTG_DATA_DIR"):
        s.data_dir = Path(env["OCTG_DATA_DIR"])
    if env.get("OCTG_EXTENSION_ID") is not None:
        s.extension_id = env["OCTG_EXTENSION_ID"]
    if env.get("OCTG_LOG_LEVEL"):
        s.log_level = env["OCTG_LOG_LEVEL"]
    if s.host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(f"Refusing to bind to {s.host!r}: the agent only listens on the loopback interface.")
    return s


@dataclass(frozen=True)
class Secrets:
    pairing_token: str  # the Chrome extension presents this in its WebSocket hello
    api_token: str  # the CLI / local callers present this on HTTP requests


def load_secrets(settings: Settings) -> Secrets:
    """Create the two random tokens on first run, then reuse them."""
    settings.ensure_dirs()
    p = settings.secrets_path
    if p.is_file():
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("pairing_token") and d.get("api_token"):
            return Secrets(d["pairing_token"], d["api_token"])
    s = Secrets(_secrets.token_urlsafe(24), _secrets.token_urlsafe(24))
    p.write_text(json.dumps({"pairing_token": s.pairing_token, "api_token": s.api_token}, indent=2), encoding="utf-8")
    return s
