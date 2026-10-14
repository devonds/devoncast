"""Settings loaded from ~/.config/devoncast/config.toml."""

from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("DEVONCAST_CONFIG_DIR", "~/.config/devoncast")).expanduser()
CONFIG_PATH = CONFIG_DIR / "config.toml"

TEMPLATE = """\
# Devoncast settings. Contains secrets: keep this file mode 600 and out of git.

home = "~/devoncast"          # episode working directories live under <home>/episodes

[podcast]
title = "Devoncast"
author = "Devon Sampson"
email = "devonds@gmail.com"
description = "Documents, briefings and articles, read aloud."
language = "en"

[tts]
voice = "af_heart"
speed = 1.0

[llm]
# "claude" runs `claude -p` on your Claude subscription. "none" reads the cleaned text as-is.
backend = "claude"
model = ""                     # empty = Claude Code's default model

[r2]
account_id = "{account_id}"
bucket = "devoncast"
public_base_url = "https://devoncast.polyculture.net"
prefix = "{prefix}"          # unguessable path segment; changing it moves the feed URL
access_key_id = ""
secret_access_key = ""
"""


@dataclass
class Config:
    home: Path
    podcast: dict
    tts: dict
    llm: dict
    r2: dict

    @property
    def episodes_dir(self) -> Path:
        return self.home / "episodes"

    @property
    def public_root(self) -> str:
        return f"{self.r2['public_base_url'].rstrip('/')}/{self.r2['prefix']}"

    @property
    def feed_url(self) -> str:
        return f"{self.public_root}/feed.xml"


def load() -> Config:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"No config at {CONFIG_PATH}. Run `devoncast init` first.")
    data = tomllib.loads(CONFIG_PATH.read_text())
    r2 = data.get("r2", {})
    r2["access_key_id"] = os.environ.get("DEVONCAST_R2_ACCESS_KEY_ID", r2.get("access_key_id", ""))
    r2["secret_access_key"] = os.environ.get(
        "DEVONCAST_R2_SECRET_ACCESS_KEY", r2.get("secret_access_key", "")
    )
    return Config(
        home=Path(data.get("home", "~/devoncast")).expanduser(),
        podcast=data.get("podcast", {}),
        tts=data.get("tts", {}),
        llm=data.get("llm", {}),
        r2=r2,
    )


def write_template(account_id: str = "") -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(TEMPLATE.format(account_id=account_id, prefix=secrets.token_urlsafe(18)))
    CONFIG_PATH.chmod(0o600)
    return CONFIG_PATH
