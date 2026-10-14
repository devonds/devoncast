"""Settings loaded from ~/.config/devoncast/config.toml."""

from __future__ import annotations

import os
import secrets
import subprocess
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
# Credentials are read from the macOS Keychain (service "devoncast", accounts
# "r2_access_key_id" and "r2_secret_access_key"). Setting them here overrides the Keychain.
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
    for field in ("access_key_id", "secret_access_key"):
        r2[field] = (
            os.environ.get(f"DEVONCAST_R2_{field.upper()}")
            or r2.get(field)
            or _keychain(f"r2_{field}")
        )
    return Config(
        home=Path(data.get("home", "~/devoncast")).expanduser(),
        podcast=data.get("podcast", {}),
        tts=data.get("tts", {}),
        llm=data.get("llm", {}),
        r2=r2,
    )


KEYCHAIN_SERVICE = "devoncast"


def _keychain(account: str) -> str:
    """Read a secret stored with `security add-generic-password -s devoncast -a <account> -w`."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def write_template(account_id: str = "") -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(TEMPLATE.format(account_id=account_id, prefix=secrets.token_urlsafe(18)))
    CONFIG_PATH.chmod(0o600)
    return CONFIG_PATH
