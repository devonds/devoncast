"""MCP server (stdio) so Claude Desktop or Claude Code can add episodes.

Rendering takes minutes, so add tools create a queued episode, start a detached `devoncast work`
process and return at once.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import config as config_mod
from . import feed, pipeline, work
from .script import SCRIPT_GUIDE
from .storage import Store

server = MCPServer(
    name="devoncast",
    instructions=(
        "Devoncast turns text into episodes of Devon's private podcast feed. When asked to add "
        "something you wrote or can see in the conversation (an artifact, a document, notes), "
        "write the speech script yourself following the add_episode guidelines and call "
        "add_episode. When given a file path or URL you cannot read, call add_document. "
        "Rendering takes about as long as the audio itself; use episode_status to check on it."
    ),
)


def _start_work(cfg: config_mod.Config) -> None:
    """Render queued episodes in the background. A no-op if another process is already rendering."""
    logs = cfg.home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [sys.executable, "-m", "devoncast.cli", "work"],
        stdout=open(logs / "work.log", "a"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


@server.tool(description=f"""Add an episode from a speech script you have written.

Write `script` following these guidelines:

{SCRIPT_GUIDE}
`title`: specific, under 70 characters. `summary`: one or two sentences.
`show_notes`: markdown, a short paragraph then a bullet per chapter.
`voice` is optional (af_heart default; also af_bella, am_michael, bf_emma, bm_george).
Returns immediately; the episode appears in the feed when rendering finishes.""")
def add_episode(title: str, script: str, summary: str, show_notes: str = "",
                source_url: str | None = None, voice: str | None = None) -> str:
    cfg = config_mod.load()
    d = pipeline.write_script_episode(cfg, title, script, summary, show_notes, source_url,
                                      voice=voice, origin={"kind": "mcp"})
    _start_work(cfg)
    words = len(script.split())
    minutes = max(1, round(words / 150))
    return (f"Queued '{title}' ({words} words, about {minutes} minutes of audio). Episode id: {d.name}. "
            f"Rendering takes about as long as the audio, so expect it in the feed in about {minutes} minutes.")


@server.tool(description="Add an episode from a local file path or a web URL. The document is "
             "extracted, rewritten for listening by Claude Code, rendered and published. Use this "
             "when you cannot see the document's text yourself. Returns immediately; rendering takes "
             "about as long as the audio, roughly a minute per 150 words.")
def add_document(path_or_url: str, voice: str | None = None) -> str:
    cfg = config_mod.load()
    is_url = path_or_url.startswith(("http://", "https://"))
    if not is_url and not Path(path_or_url).expanduser().is_file():
        return f"No such file: {path_or_url}"
    name = path_or_url.split("://", 1)[-1] if is_url else Path(path_or_url).stem
    d = pipeline.create_episode(cfg, name, target=path_or_url, voice=voice, origin={"kind": "mcp"})
    _start_work(cfg)
    return f"Queued {path_or_url}. Episode id: {d.name}."


@server.tool(description="Check rendering progress for an episode id returned by an add tool. Failed "
             "episodes retry on their own for several hours unless the failure is permanent.",
             annotations=ToolAnnotations(readOnlyHint=True))
def episode_status(episode_id: str) -> str:
    d = pipeline.find_episode_dir(config_mod.load(), episode_id)
    line = work.describe(d)
    if pipeline.status(d).get("stage") == "failed" and (d / "log.txt").exists():
        line += "\n" + (d / "log.txt").read_text()[-1500:]
    return line


@server.tool(description="Retry a failed episode now, resuming from the last stage that finished. "
             "Also works for an episode that is waiting to retry.")
def retry_episode(episode_id: str) -> str:
    cfg = config_mod.load()
    d = pipeline.find_episode_dir(cfg, episode_id)
    work.requeue(d)
    _start_work(cfg)
    return f"Retrying {d.name}."


@server.tool(description="List published episodes, newest first.", annotations=ToolAnnotations(readOnlyHint=True))
def list_episodes() -> str:
    eps = feed.load_catalog(Store(config_mod.load()))
    if not eps:
        return "No episodes yet."
    return "\n".join(f"{e['pub_date'][:10]} | {int(e['duration']) // 60} min | {e['title']} | id: {e['slug']}"
                     for e in reversed(eps))


@server.tool(description="Remove an episode from the feed and delete its audio. Confirm with the user first.",
             annotations=ToolAnnotations(destructiveHint=True))
def remove_episode(episode_id: str) -> str:
    ep = pipeline.remove_episode(config_mod.load(), episode_id)
    return f"Removed '{ep['title']}'."


@server.tool(description="The podcast feed URL to subscribe to in a podcast app.",
             annotations=ToolAnnotations(readOnlyHint=True))
def feed_url() -> str:
    return config_mod.load().feed_url


def serve() -> None:
    server.run("stdio")
