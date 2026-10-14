"""MCP server (stdio) so Claude Desktop or Claude Code can add episodes.

Rendering takes minutes, so add tools start a detached `devoncast` process and return at once.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import config as config_mod
from . import feed, pipeline
from .script import SCRIPT_GUIDE
from .storage import Store

server = MCPServer(
    name="devoncast",
    instructions=(
        "Devoncast turns text into episodes of Devon's private podcast feed. When asked to add "
        "something you wrote or can see in the conversation (an artifact, a document, notes), "
        "write the speech script yourself following the add_episode guidelines and call "
        "add_episode. When given a file path or URL you cannot read, call add_document. "
        "Episodes take a few minutes to render; use episode_status to check on them."
    ),
)


def _spawn(d: Path, args: list[str]) -> None:
    log = open(d / "log.txt", "a")
    subprocess.Popen(
        [sys.executable, "-m", "devoncast.cli", *args],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
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
    d = pipeline.write_script_episode(cfg, title, script, summary, show_notes, source_url)
    args = ["redo", str(d), "--from", "synthesize"] + (["--voice", voice] if voice else [])
    _spawn(d, args)
    words = len(script.split())
    return (f"Queued '{title}' ({words} words, about {words // 150} minutes of audio). "
            f"Episode id: {d.name}. Rendering usually takes a quarter to a half of the audio length.")


@server.tool(description="Add an episode from a local file path or a web URL. The document is "
             "extracted, rewritten for listening by Claude Code, rendered and published. Use this "
             "when you cannot see the document's text yourself. Returns immediately.")
def add_document(path_or_url: str, voice: str | None = None) -> str:
    cfg = config_mod.load()
    is_url = path_or_url.startswith(("http://", "https://"))
    if not is_url and not Path(path_or_url).expanduser().exists():
        return f"No such file: {path_or_url}"
    name = path_or_url if is_url else Path(path_or_url).stem
    d = pipeline.new_episode_dir(cfg, name)
    pipeline.set_status(d, "queued")
    _spawn(d, ["add", path_or_url, "--episode-dir", str(d)] + (["--voice", voice] if voice else []))
    return f"Queued {path_or_url}. Episode id: {d.name}."


@server.tool(description="Check rendering progress for an episode id returned by an add tool.",
             annotations=ToolAnnotations(readOnlyHint=True))
def episode_status(episode_id: str) -> str:
    d = pipeline.find_episode_dir(config_mod.load(), episode_id)
    status = json.loads((d / "status.json").read_text()) if (d / "status.json").exists() else {}
    if status.get("stage") == "failed":
        tail = (d / "log.txt").read_text()[-1500:] if (d / "log.txt").exists() else ""
        return f"Failed: {status.get('error')}\n{tail}"
    if status.get("stage") == "synthesize" and status.get("total"):
        return f"Synthesizing: {status['done']} of {status['total']} paragraphs"
    return f"Stage: {status.get('stage', 'unknown')}"


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
