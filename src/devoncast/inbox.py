"""Drop-folder intake: every file put in <home>/Inbox becomes an episode.

launchd runs `devoncast inbox` whenever the folder changes, and every few minutes so retries come
due. A file moves to processing/ while its episode renders (and while it waits to retry), then to
done/ or failed/. A .webloc (a URL dragged from a browser) adds that web page.
"""

from __future__ import annotations

import plistlib
import shutil
import time
from pathlib import Path

from . import pipeline, work
from .config import Config
from .notify import notify

SUPPORTED = {".md", ".markdown", ".txt", ".html", ".htm", ".docx", ".rtf", ".odt", ".epub", ".webloc"}


def inbox_dir(cfg: Config) -> Path:
    return cfg.home / "Inbox"


def process(cfg: Config) -> None:
    inbox = inbox_dir(cfg)
    for sub in ("processing", "done", "failed"):
        (inbox / sub).mkdir(parents=True, exist_ok=True)
    for path in _orphans(cfg, inbox):
        accept(cfg, path, inbox)
    while files := sorted(p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")):
        for path in files:
            if path.suffix.lower() not in SUPPORTED:
                _move(path, inbox / "failed")
                notify("Devoncast", f"Skipped {path.name}: unsupported file type")
            elif _settled(path):  # otherwise still being copied; the next pass picks it up
                accept(cfg, _move(path, inbox / "processing"), inbox)
    work.work(cfg)


def accept(cfg: Config, path: Path, inbox: Path) -> None:
    """Create a queued episode for a file in processing/."""
    target = webloc_url(path) if path.suffix.lower() == ".webloc" else str(path)
    try:
        pipeline.create_episode(cfg, path.stem, target=target, origin={"kind": "inbox", "file": path.name})
    except Exception as e:  # noqa: BLE001 - report it and keep going with the other files
        _move(path, inbox / "failed")
        notify("Devoncast failed", f"{path.name}: {e}")


def episode_changed(cfg: Config, d: Path) -> None:
    """Move the episode's Inbox file to done/ or failed/ once the episode is finished."""
    st = pipeline.status(d)
    if st.get("stage") in pipeline.FINISHED:
        dest = "done"
    elif st.get("stage") == "failed" and "retry_at" not in st:
        dest = "failed"
    else:
        return
    inbox = inbox_dir(cfg)
    name = pipeline.request(d)["origin"]["file"]
    for sub in ("processing", "failed"):  # failed/ too: a manual retry can succeed
        if (inbox / sub / name).exists() and sub != dest:
            _move(inbox / sub / name, inbox / dest)
            return


def webloc_url(path: Path) -> str:
    return plistlib.loads(path.read_bytes())["URL"]


def _orphans(cfg: Config, inbox: Path) -> list[Path]:
    """Files in processing/ with no episode, left by a crash between moving and accepting."""
    claimed = {
        o.get("file")
        for d in cfg.episodes_dir.glob("*")
        if (o := pipeline.request(d).get("origin", {})).get("kind") == "inbox"
    } if cfg.episodes_dir.exists() else set()
    return [p for p in sorted((inbox / "processing").iterdir())
            if p.is_file() and not p.name.startswith(".") and p.name not in claimed]


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
