"""Drop-folder intake: every file put in <home>/Inbox becomes an episode.

launchd runs `devoncast inbox` whenever the folder changes. Files move to processing/ while they
render, then to done/ or failed/. A .webloc (a URL dragged from a browser) adds that web page.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import time
from pathlib import Path

from . import pipeline
from .config import Config

SUPPORTED = {".md", ".markdown", ".txt", ".html", ".htm", ".docx", ".rtf", ".odt", ".epub", ".webloc"}


def inbox_dir(cfg: Config) -> Path:
    return cfg.home / "Inbox"


def process(cfg: Config) -> None:
    inbox = inbox_dir(cfg)
    for sub in ("processing", "done", "failed"):
        (inbox / sub).mkdir(parents=True, exist_ok=True)
    while True:
        files = sorted(p for p in inbox.iterdir() if p.is_file() and not p.name.startswith("."))
        if not files:
            return
        for path in files:
            if path.suffix.lower() not in SUPPORTED:
                _move(path, inbox / "failed")
                notify("Devoncast", f"Skipped {path.name}: unsupported file type")
                continue
            if not _settled(path):
                continue  # still being copied; the next folder change will pick it up
            working = _move(path, inbox / "processing")
            add_one(cfg, working, inbox)


def add_one(cfg: Config, working: Path, inbox: Path) -> None:
    target = webloc_url(working) if working.suffix.lower() == ".webloc" else str(working)
    d = pipeline.new_episode_dir(cfg, working.stem)
    try:
        meta = pipeline.run(cfg, d, target=target)
    except Exception as e:  # noqa: BLE001 - report every failure, keep processing the rest
        _move(working, inbox / "failed")
        notify("Devoncast failed", f"{working.name}: {e}")
        return
    _move(working, inbox / "done")
    notify("New Devoncast episode", meta["title"])


def webloc_url(path: Path) -> str:
    return plistlib.loads(path.read_bytes())["URL"]


def _settled(path: Path, wait: float = 1.0) -> bool:
    size = path.stat().st_size
    time.sleep(wait)
    return path.exists() and path.stat().st_size == size


def _move(path: Path, dest_dir: Path) -> Path:
    dest = dest_dir / path.name
    if dest.exists():
        dest = dest_dir / f"{path.stem}-{int(time.time())}{path.suffix}"
    shutil.move(path, dest)
    return dest


def notify(title: str, message: str) -> None:
    script = f"display notification {_applescript_str(message)} with title {_applescript_str(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True)


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"')[:200] + '"'
