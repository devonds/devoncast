"""devoncast command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config as config_mod
from . import pipeline

R2_ACCOUNT_ID = "8145a067bb67da1fd30f9ffbc9ed70a2"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="devoncast", description="Turn documents into podcast episodes.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create config, cover art and an empty feed")

    add = sub.add_parser("add", help="add a file or URL as a new episode")
    add.add_argument("target", help="path to .md/.txt/.html/.docx, or an http(s) URL")
    add.add_argument("--episode-dir", help=argparse.SUPPRESS)
    _render_args(add)

    redo = sub.add_parser("redo", help="rerun an existing episode from a stage")
    redo.add_argument("episode", help="episode directory or part of its name")
    redo.add_argument("--from", dest="from_stage", choices=pipeline.STAGES, default="synthesize")
    _render_args(redo)

    sub.add_parser("list", help="list published episodes")
    rm = sub.add_parser("remove", help="remove an episode from the feed and delete its audio")
    rm.add_argument("episode", help="guid or slug")
    sub.add_parser("rebuild-feed", help="regenerate feed.xml from episodes.json")
    sub.add_parser("feed-url", help="print the feed URL to subscribe to")
    st = sub.add_parser("status", help="show progress of local episodes")
    st.add_argument("episode", nargs="?")

    voices = sub.add_parser("voices", help="render a sample of each voice to compare")
    voices.add_argument("text", nargs="?", default=(
        "Welcome to Devoncast. Today's episode covers how to evaluate a churn model, "
        "from ROC curves to calibration, and why accuracy is useless when only five percent of customers leave."))
    voices.add_argument("--out", default="~/devoncast/voice-samples")

    sub.add_parser("mcp", help="run the MCP server over stdio")
    sub.add_parser("inbox", help="turn every file in the Inbox folder into an episode")
    sub.add_parser("poll", help="render jobs queued through the remote MCP server")
    inst = sub.add_parser("install", help="set up the Inbox folder, launchd agents and Finder Quick Action")
    inst.add_argument("--no-poll", action="store_true", help="skip the remote-queue poller")
    sub.add_parser("uninstall", help="remove the launchd agents and Quick Action")

    args = parser.parse_args(argv)
    handler = globals()[f"cmd_{args.cmd.replace('-', '_')}"]
    handler(args)


def _render_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--voice", help="Kokoro voice, e.g. af_heart, am_michael, bf_emma")
    p.add_argument("--no-llm", action="store_true", help="skip the Claude rewrite; read the cleaned text")
    p.add_argument("--no-publish", action="store_true", help="render the MP3 locally without uploading")


def cmd_init(args) -> None:
    if not config_mod.CONFIG_PATH.exists():
        path = config_mod.write_template(R2_ACCOUNT_ID)
        print(f"Wrote {path}. Add your R2 access_key_id and secret_access_key, then run `devoncast init` again.")
        return
    cfg = config_mod.load()
    cfg.episodes_dir.mkdir(parents=True, exist_ok=True)
    from . import cover, feed
    from .storage import SHORT_CACHE, Store

    store = Store(cfg)
    cover_path = config_mod.CONFIG_DIR / "cover.png"
    if not cover_path.exists():
        cover.make_cover(cover_path, cfg.podcast.get("title", "Devoncast"))
    store.put_file(feed.COVER, cover_path, "image/png", cache=SHORT_CACHE)
    url = feed.save_and_publish(store, cfg, feed.load_catalog(store))
    print(f"Feed published. Subscribe to:\n  {url}")


def cmd_add(args) -> None:
    cfg = config_mod.load()
    name = args.target
    if not name.startswith(("http://", "https://")):
        path = Path(name).expanduser()
        if not path.exists():
            sys.exit(f"No such file: {path}")
        name = path.stem
    d = Path(args.episode_dir) if args.episode_dir else pipeline.new_episode_dir(cfg, name)
    print(f"episode dir: {d}")
    pipeline.run(cfg, d, target=args.target, use_llm=not args.no_llm, voice=args.voice, publish=not args.no_publish)
    _print_done(cfg, d, args.no_publish)


def cmd_redo(args) -> None:
    cfg = config_mod.load()
    d = pipeline.find_episode_dir(cfg, args.episode)
    pipeline.reset_from(d, args.from_stage)
    pipeline.run(cfg, d, use_llm=not args.no_llm, voice=args.voice, publish=not args.no_publish)
    _print_done(cfg, d, args.no_publish)


def _print_done(cfg, d: Path, local_only: bool) -> None:
    print(f"audio: {d / 'episode.mp3'}")
    if not local_only:
        print(f"feed: {cfg.feed_url}")


def cmd_list(args) -> None:
    from . import feed
    from .storage import Store

    cfg = config_mod.load()
    for ep in reversed(feed.load_catalog(Store(cfg))):
        print(f"{ep['pub_date'][:10]}  {int(ep['duration']) // 60:>3} min  {ep['title']}\n            {ep['slug']}")


def cmd_remove(args) -> None:
    ep = pipeline.remove_episode(config_mod.load(), args.episode)
    print(f"removed: {ep['title']}")


def cmd_rebuild_feed(args) -> None:
    from . import feed
    from .storage import Store

    cfg = config_mod.load()
    store = Store(cfg)
    print(feed.save_and_publish(store, cfg, feed.load_catalog(store)))


def cmd_feed_url(args) -> None:
    print(config_mod.load().feed_url)


def cmd_status(args) -> None:
    cfg = config_mod.load()
    dirs = [pipeline.find_episode_dir(cfg, args.episode)] if args.episode else sorted(cfg.episodes_dir.iterdir())[-10:]
    for d in dirs:
        status = d / "status.json"
        print(f"{d.name}: {status.read_text() if status.exists() else 'no status'}")


def cmd_voices(args) -> None:
    from . import tts
    import soundfile as sf

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    for voice in tts.VOICES:
        path = out / f"{voice}.wav"
        sf.write(path, tts.speak(args.text, voice), tts.SAMPLE_RATE)
        print(path)


def cmd_mcp(args) -> None:
    from .mcp_server import serve

    serve()


def cmd_inbox(args) -> None:
    from . import inbox

    inbox.process(config_mod.load())


def cmd_poll(args) -> None:
    from . import remote

    remote.poll(config_mod.load())


def cmd_install(args) -> None:
    from . import install

    for line in install.install(config_mod.load(), poll=not args.no_poll):
        print(f"installed {line}")


def cmd_uninstall(args) -> None:
    from . import install

    install.uninstall()
    print("removed launchd agents and Quick Action")


if __name__ == "__main__":
    main()
