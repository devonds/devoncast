"""Runs the stages for one episode inside its working directory.

An episode directory is the durable record of one request. request.json says what to build,
status.json says how far it got, and each stage's output file marks that stage done. Every
intake creates a fresh directory, so two requests never share one. A run that stops for any
reason (an error, a crash, a reboot) resumes after the last finished stage. `reset_from`
discards a stage's output and everything after it, for deliberate reruns.
"""

from __future__ import annotations

import fcntl
import hashlib
import itertools
import json
import re
import shutil
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from . import audio, extract, feed, prep, script, tts
from .config import Config
from .errors import PermanentError
from .storage import Store

STAGES = ["extract", "prep", "script", "synthesize", "publish"]
OUTPUTS = {
    "extract": ["source.md", "source.json"],
    "prep": ["cleaned.md"],
    "script": ["script.txt", "meta.json"],
    "synthesize": ["episode.mp3", "chapters.json"],
    "publish": ["published.json"],
}
ACTIVE = {"starting", *STAGES}  # a run is in progress, or died during one
FINISHED = {"done", "rendered"}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "episode"


def new_episode_dir(cfg: Config, name: str) -> Path:
    """A new, empty directory. A name already used today gets a numeric suffix."""
    base = f"{date.today().isoformat()}-{slugify(name)}"
    cfg.episodes_dir.mkdir(parents=True, exist_ok=True)
    for n in itertools.count(1):
        d = cfg.episodes_dir / (base if n == 1 else f"{base}-{n}")
        try:
            d.mkdir()
            return d
        except FileExistsError:
            continue
    raise AssertionError("unreachable")


def create_episode(cfg: Config, name: str, *, target: str | None = None, voice: str | None = None,
                   use_llm: bool = True, publish: bool = True, origin: dict | None = None) -> Path:
    """A new episode directory holding everything needed to render it later, marked queued.

    A local input file is copied in, so the episode doesn't depend on where the file was.
    """
    d = new_episode_dir(cfg, name)
    if target and not extract.is_url(target):
        src = Path(target).expanduser()
        if not src.is_file():
            shutil.rmtree(d)
            raise PermanentError(f"No such file: {src}")
        (d / "input").mkdir()
        shutil.copy2(src, d / "input" / src.name)
        target = f"input/{src.name}"
    save_request(d, {"name": name, "target": target, "voice": voice, "use_llm": use_llm,
                     "publish": publish, "origin": origin or {"kind": "cli"}})
    set_status(d, "queued")
    return d


def find_episode_dir(cfg: Config, ref: str) -> Path:
    path = Path(ref).expanduser()
    if path.is_dir():
        return path
    if (cfg.episodes_dir / ref).is_dir():
        return cfg.episodes_dir / ref
    matches = sorted(cfg.episodes_dir.glob(f"*{ref}*"))
    if len(matches) != 1:
        raise SystemExit(f"'{ref}' matches {len(matches)} episode directories")
    return matches[0]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def request(d: Path) -> dict:
    return read_json(d / "request.json")


def save_request(d: Path, req: dict) -> None:
    (d / "request.json").write_text(json.dumps(req, indent=2))


def status(d: Path) -> dict:
    return read_json(d / "status.json")


def set_status(d: Path, stage: str, **extra) -> None:
    """Replace the status, keeping the attempt count unless `extra` sets it."""
    st = {"stage": stage, "updated": time.time(), "attempts": status(d).get("attempts", 0), **extra}
    (d / "status.json").write_text(json.dumps(st))


def reset_from(d: Path, stage: str) -> None:
    for s in STAGES[STAGES.index(stage):]:
        for name in OUTPUTS[s]:
            (d / name).unlink(missing_ok=True)


@contextmanager
def _file_lock(path: Path, wait: bool) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def render_lock(cfg: Config, wait: bool = True):
    """One episode at a time, because TTS saturates the CPU. Yields whether the lock was taken."""
    return _file_lock(cfg.home / ".render.lock", wait)


def catalog_lock(cfg: Config):
    """Guards the read-modify-write of episodes.json in R2."""
    return _file_lock(cfg.home / ".catalog.lock", wait=True)


def run(cfg: Config, d: Path, *, target: str | None = None, use_llm: bool | None = None,
        voice: str | None = None, publish: bool | None = None, log=print) -> dict:
    """Render and publish one episode, waiting for the render lock. Arguments given here are
    saved into request.json, so a later resume uses them too."""
    given = {"target": target, "use_llm": use_llm, "voice": voice, "publish": publish}
    if any(v is not None for v in given.values()):
        save_request(d, request(d) | {k: v for k, v in given.items() if v is not None})
    with render_lock(cfg):
        return run_locked(cfg, d, log)


def run_locked(cfg: Config, d: Path, log=print) -> dict:
    """`run` for a caller that already holds the render lock. Marks the episode failed on error."""
    req = request(d)
    try:
        meta = _run(cfg, d, req, log)
    except Exception as e:
        set_status(d, "failed", error=str(e))
        raise
    set_status(d, "done" if req.get("publish", True) else "rendered")
    return meta


def _run(cfg: Config, d: Path, req: dict, log) -> dict:
    if not (d / "source.md").exists():
        target = req.get("target")
        if not target:
            raise PermanentError(f"{d.name} has no source.md and no input to extract")
        if not extract.is_url(target) and not Path(target).is_absolute():
            target = str(d / target)
        set_status(d, "extract")
        src = extract.extract(target)
        (d / "source.md").write_text(src.markdown)
        (d / "source.json").write_text(json.dumps(
            {"name": req.get("name") or src.name, "url": src.url, "input": req.get("target")}))
        log(f"extracted {len(src.markdown.split())} words")

    if not (d / "cleaned.md").exists():
        set_status(d, "prep")
        (d / "cleaned.md").write_text(prep.clean_markdown((d / "source.md").read_text()))

    if not (d / "script.txt").exists():
        set_status(d, "script")
        source_info = json.loads((d / "source.json").read_text())
        cleaned = (d / "cleaned.md").read_text()
        use_llm = req.get("use_llm", True)
        if use_llm and cfg.llm.get("backend", "claude") == "claude" and script.claude_available():
            log("writing script with claude -p (this takes a minute or two)")
            sc = script.write_script(cleaned, cfg.llm.get("model", ""))
        else:
            log("no LLM: reading cleaned text as-is")
            first = next((l[2:] for l in cleaned.splitlines() if l.startswith("# ")), source_info["name"])
            sc = script.Script(first, f"An audio edition of {first}.", "", prep.fallback_script(cleaned))
        meta = sc.to_dict()
        meta.pop("script")
        meta["source_url"] = source_info.get("url")
        (d / "meta.json").write_text(json.dumps(meta, indent=2))
        (d / "script.txt").write_text(sc.script)
        log(f"title: {sc.title}")

    meta = json.loads((d / "meta.json").read_text())

    if not (d / "episode.mp3").exists():
        voice = req.get("voice") or cfg.tts.get("voice", "af_heart")
        chapters = prep.parse_script((d / "script.txt").read_text())
        words = sum(len(p.split()) for c in chapters for p in c.paragraphs)
        log(f"synthesizing {len(chapters)} chapters, {words} words, voice {voice}")
        started = time.time()

        def progress(done, total):
            set_status(d, "synthesize", done=done, total=total)

        rendered = tts.render(chapters, voice, float(cfg.tts.get("speed", 1.0)), progress)
        audio.encode_mp3(rendered, d / "episode.mp3", meta["title"], cfg.podcast.get("author", ""),
                         cfg.podcast.get("title", "Devoncast"))
        (d / "chapters.json").write_text(json.dumps(
            {"duration": rendered.duration, "voice": voice, "chapters": rendered.chapter_starts}))
        log(f"rendered {rendered.duration / 60:.1f} min of audio in {(time.time() - started) / 60:.1f} min")

    if req.get("publish", True) and not (d / "published.json").exists():
        set_status(d, "publish")
        ep = publish_episode(cfg, d, meta)
        log(f"published: {ep['title']}")

    return meta


def publish_episode(cfg: Config, d: Path, meta: dict) -> dict:
    store = Store(cfg)
    mp3 = d / "episode.mp3"
    rendered = json.loads((d / "chapters.json").read_text())
    digest = hashlib.sha256(mp3.read_bytes()).hexdigest()[:10]
    slug = d.name
    audio_name = f"episodes/{slug}-{digest}.mp3"
    url = store.put_file(audio_name, mp3, "audio/mpeg")

    with catalog_lock(cfg):
        episodes = feed.load_catalog(store)
        existing = next((e for e in episodes if e["slug"] == slug), None)
        ep = {
            "guid": existing["guid"] if existing else str(uuid.uuid4()),
            "slug": slug,
            "title": meta["title"],
            "summary": meta["summary"],
            "show_notes": meta.get("show_notes", ""),
            "source_url": meta.get("source_url"),
            "pub_date": existing["pub_date"] if existing else datetime.now(timezone.utc).isoformat(),
            "audio_name": audio_name,
            "audio_url": url,
            "bytes": mp3.stat().st_size,
            "duration": rendered["duration"],
            "voice": rendered.get("voice"),
        }
        if existing:
            episodes.remove(existing)
        episodes.append(ep)
        feed.save_and_publish(store, cfg, episodes)
    # Only drop the old audio once the feed no longer points at it.
    if existing and existing["audio_name"] != audio_name:
        store.delete(existing["audio_name"])
    (d / "published.json").write_text(json.dumps(ep, indent=2))
    return ep


def remove_episode(cfg: Config, ref: str) -> dict:
    store = Store(cfg)
    with catalog_lock(cfg):
        episodes = feed.load_catalog(store)
        matches = [e for e in episodes if ref in (e["guid"], e["slug"]) or ref in e["slug"]]
        exact = [e for e in matches if ref in (e["guid"], e["slug"])]
        matches = exact or matches
        if len(matches) != 1:
            raise SystemExit(f"'{ref}' matches {len(matches)} published episodes")
        ep = matches[0]
        episodes.remove(ep)
        feed.save_and_publish(store, cfg, episodes)
    store.delete(ep["audio_name"])
    local = cfg.episodes_dir / ep["slug"] / "published.json"
    local.unlink(missing_ok=True)
    return ep


def write_script_episode(cfg: Config, title: str, script_text: str, summary: str,
                         show_notes: str = "", source_url: str | None = None, *,
                         voice: str | None = None, origin: dict | None = None) -> Path:
    """Create a queued episode from a finished script, skipping extract/prep/script."""
    d = create_episode(cfg, title, voice=voice, origin=origin)
    (d / "source.md").write_text(script_text)
    (d / "source.json").write_text(json.dumps({"name": title, "url": source_url, "input": "script"}))
    (d / "cleaned.md").write_text(script_text)
    (d / "script.txt").write_text(script_text)
    (d / "meta.json").write_text(json.dumps(
        {"title": title, "summary": summary, "show_notes": show_notes, "source_url": source_url}, indent=2))
    return d
