"""Runs the stages for one episode inside its working directory.

Each stage writes its output file and is skipped when that file already exists, so rerunning a
command resumes where it stopped. `from_stage` discards that stage's output and everything after.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from . import audio, extract, feed, prep, script, tts
from .config import Config
from .storage import Store

STAGES = ["extract", "prep", "script", "synthesize", "publish"]
OUTPUTS = {
    "extract": ["source.md", "source.json"],
    "prep": ["cleaned.md"],
    "script": ["script.txt", "meta.json"],
    "synthesize": ["episode.mp3", "chapters.json"],
    "publish": ["published.json"],
}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "episode"


def new_episode_dir(cfg: Config, name: str) -> Path:
    d = cfg.episodes_dir / f"{date.today().isoformat()}-{slugify(name)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_episode_dir(cfg: Config, ref: str) -> Path:
    path = Path(ref).expanduser()
    if path.is_dir():
        return path
    matches = sorted(cfg.episodes_dir.glob(f"*{ref}*"))
    if len(matches) != 1:
        raise SystemExit(f"'{ref}' matches {len(matches)} episode directories")
    return matches[0]


def set_status(d: Path, stage: str, **extra) -> None:
    (d / "status.json").write_text(json.dumps({"stage": stage, "updated": time.time(), **extra}))


def reset_from(d: Path, stage: str) -> None:
    for s in STAGES[STAGES.index(stage):]:
        for name in OUTPUTS[s]:
            (d / name).unlink(missing_ok=True)


@contextmanager
def render_lock(cfg: Config):
    """One episode at a time: TTS saturates the CPU, and publishing rewrites the shared catalog."""
    cfg.home.mkdir(parents=True, exist_ok=True)
    with open(cfg.home / ".render.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def run(cfg: Config, d: Path, *, target: str | None = None, use_llm: bool = True,
        voice: str | None = None, publish: bool = True, log=print) -> dict:
    if not (d / "status.json").exists():
        set_status(d, "queued")
    try:
        with render_lock(cfg):
            return _run(cfg, d, target=target, use_llm=use_llm, voice=voice, publish=publish, log=log)
    except Exception as e:
        set_status(d, "failed", error=str(e))
        raise


def _run(cfg, d, *, target, use_llm, voice, publish, log) -> dict:
    if not (d / "source.md").exists():
        if target is None:
            raise RuntimeError(f"{d} has no source.md and no input was given")
        set_status(d, "extract")
        src = extract.extract(target)
        (d / "source.md").write_text(src.markdown)
        (d / "source.json").write_text(json.dumps({"name": src.name, "url": src.url, "input": target}))
        log(f"extracted {len(src.markdown.split())} words")

    if not (d / "cleaned.md").exists():
        set_status(d, "prep")
        (d / "cleaned.md").write_text(prep.clean_markdown((d / "source.md").read_text()))

    if not (d / "script.txt").exists():
        set_status(d, "script")
        source_info = json.loads((d / "source.json").read_text())
        cleaned = (d / "cleaned.md").read_text()
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
        voice = voice or cfg.tts.get("voice", "af_heart")
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

    if publish and not (d / "published.json").exists():
        set_status(d, "publish")
        ep = publish_episode(cfg, d, meta)
        log(f"published: {ep['title']}")

    set_status(d, "done" if publish else "rendered")
    return meta


def publish_episode(cfg: Config, d: Path, meta: dict) -> dict:
    store = Store(cfg)
    mp3 = d / "episode.mp3"
    rendered = json.loads((d / "chapters.json").read_text())
    digest = hashlib.sha256(mp3.read_bytes()).hexdigest()[:10]
    slug = d.name
    audio_name = f"episodes/{slug}-{digest}.mp3"

    episodes = feed.load_catalog(store)
    existing = next((e for e in episodes if e["slug"] == slug), None)
    url = store.put_file(audio_name, mp3, "audio/mpeg")
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
        if existing["audio_name"] != audio_name:
            store.delete(existing["audio_name"])
        episodes.remove(existing)
    episodes.append(ep)
    feed.save_and_publish(store, cfg, episodes)
    (d / "published.json").write_text(json.dumps(ep, indent=2))
    return ep


def remove_episode(cfg: Config, ref: str) -> dict:
    store = Store(cfg)
    episodes = feed.load_catalog(store)
    matches = [e for e in episodes if ref in (e["guid"], e["slug"]) or ref in e["slug"]]
    if len(matches) != 1:
        raise SystemExit(f"'{ref}' matches {len(matches)} published episodes")
    ep = matches[0]
    store.delete(ep["audio_name"])
    episodes.remove(ep)
    feed.save_and_publish(store, cfg, episodes)
    local = cfg.episodes_dir / ep["slug"] / "published.json"
    local.unlink(missing_ok=True)
    return ep


def write_script_episode(cfg: Config, title: str, script_text: str, summary: str,
                         show_notes: str = "", source_url: str | None = None) -> Path:
    """Create an episode directory from a finished script, skipping extract/prep/script."""
    d = new_episode_dir(cfg, title)
    reset_from(d, "extract")
    (d / "source.md").write_text(script_text)
    (d / "source.json").write_text(json.dumps({"name": title, "url": source_url, "input": "mcp"}))
    (d / "cleaned.md").write_text(script_text)
    (d / "script.txt").write_text(script_text)
    (d / "meta.json").write_text(json.dumps(
        {"title": title, "summary": summary, "show_notes": show_notes, "source_url": source_url}, indent=2))
    set_status(d, "queued")
    return d

