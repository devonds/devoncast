"""Episode catalog (episodes.json in R2) and the RSS feed generated from it."""

from __future__ import annotations

import json
from datetime import datetime

from feedgen.feed import FeedGenerator
from markdown_it import MarkdownIt

from .config import Config
from .storage import Store

CATALOG = "episodes.json"
FEED = "feed.xml"
COVER = "cover.png"

_md = MarkdownIt("commonmark")


def load_catalog(store: Store) -> list[dict]:
    raw = store.get_bytes(CATALOG)
    return json.loads(raw) if raw else []


def save_and_publish(store: Store, cfg: Config, episodes: list[dict]) -> str:
    episodes.sort(key=lambda e: e["pub_date"])
    # Feed first: if it fails, the catalog still describes what listeners see, so a retry knows
    # which old audio file to clean up.
    url = store.put_bytes(FEED, build_feed(cfg, episodes), "application/rss+xml; charset=utf-8")
    store.put_bytes(CATALOG, json.dumps(episodes, indent=2).encode(), "application/json")
    return url


def build_feed(cfg: Config, episodes: list[dict]) -> bytes:
    p = cfg.podcast
    fg = FeedGenerator()
    fg.load_extension("podcast")
    fg.title(p.get("title", "Devoncast"))
    fg.description(p.get("description", "Documents read aloud."))
    fg.link(href=cfg.feed_url, rel="self")
    fg.link(href=cfg.public_root + "/", rel="alternate")
    fg.language(p.get("language", "en"))
    fg.author({"name": p.get("author", ""), "email": p.get("email", "")})
    fg.image(f"{cfg.public_root}/{COVER}", title=p.get("title", "Devoncast"), link=cfg.public_root + "/")
    fg.podcast.itunes_author(p.get("author", ""))
    fg.podcast.itunes_owner(name=p.get("author", ""), email=p.get("email", ""))
    fg.podcast.itunes_image(f"{cfg.public_root}/{COVER}")
    fg.podcast.itunes_category("Education")
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_block(True)  # ask directories not to list this feed

    for ep in episodes:  # feedgen prepends entries, so oldest first yields newest at top
        fe = fg.add_entry()
        fe.id(ep["guid"])
        fe.guid(ep["guid"], permalink=False)
        fe.title(ep["title"])
        fe.description(_notes_html(ep))
        fe.published(datetime.fromisoformat(ep["pub_date"]))
        fe.enclosure(ep["audio_url"], str(ep["bytes"]), "audio/mpeg")
        if ep.get("source_url"):
            fe.link(href=ep["source_url"])
        fe.podcast.itunes_duration(int(ep["duration"]))
        fe.podcast.itunes_summary(ep["summary"])
    return fg.rss_str(pretty=True)


def _notes_html(ep: dict) -> str:
    notes = ep.get("show_notes") or ep["summary"]
    if ep.get("source_url"):
        notes += f"\n\nSource: <{ep['source_url']}>"
    return _md.render(notes)
