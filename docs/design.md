# Devoncast design

Turn text documents (markdown, plain text, HTML, web articles) into spoken MP3s and publish them
as episodes in a private podcast feed that a phone podcast app can subscribe to.

## Goals

- One command (or one sentence to Claude) adds an episode: `devoncast add <file-or-url>`.
- Local, open TTS. No per-character API bills.
- LLM work (rewriting for the ear, titles, show notes) runs on the Claude subscription, never on
  paid API calls.
- Feed and audio hosted on Cloudflare R2. Nothing else to run or pay for.
- Adding an episode never breaks existing ones. The feed only grows unless an episode is removed.

## Pipeline

```
input (.md / .txt / .html / URL)
  1. extract     -> source.md       file read as-is; HTML and URLs via trafilatura
  2. prep        -> cleaned.md      deterministic markdown pass: drop code blocks, images,
                                    link targets, HTML; keep headings as chapter boundaries
  3. script      -> script.txt      LLM rewrite for listening, plus title, summary, show notes
                    meta.json       (claude -p, or done by Claude itself when called over MCP)
  4. synthesize  -> episode.mp3     Kokoro TTS per paragraph, pauses between paragraphs and
                                    chapters, ffmpeg encode with ID3 tags and chapter markers
  5. publish     -> R2              upload MP3, update episodes.json, rebuild and upload feed.xml
```

Each request gets its own working directory, `~/devoncast/episodes/<date>-<slug>/` (a name used
earlier the same day gets `-2`, `-3`), holding every stage's output. Stages skip when their output
already exists, so a voice change only reruns step 4 onward (`devoncast redo <ep> --from synthesize`).

## Jobs and failure recovery

The episode directory is the durable record of one request:

- `request.json`: what to build (input, voice, whether to use the LLM and publish) and where the
  request came from (`origin`: inbox file, remote job id, MCP, CLI). A local input file is copied
  into `input/`, so the episode doesn't depend on where the file was.
- `status.json`: the current stage, the attempt count, and after a failure the error and
  `retry_at`.
- `log.txt`: every attempt's log and traceback.

Intakes only create a queued directory. `devoncast work` (`work.py`) renders every episode that is
queued, due for a retry, or left in a running stage by a process that died, one at a time under
the render lock. Each attempt resumes after the last finished stage, so retrying a failed upload
doesn't redo the TTS. A failure is retried after 2 min, 10 min, 30 min, 2 h and 6 h (six attempts
over about nine hours, enough to outlast a Claude usage limit). A `PermanentError` (unsupported
file, no readable text, missing input) fails at once. `devoncast retry <ep>`, the local MCP
`retry_episode` tool and the remote `retry_job` tool start a fresh set of attempts.

## Components

| Module | Job |
| --- | --- |
| `extract.py` | Path or URL to markdown text |
| `prep.py` | markdown-it-py AST walk to clean text with chapter headings |
| `script.py` | Prompt and parser for the LLM rewrite; shells out to `claude -p` |
| `tts.py` | Kokoro (PyTorch, `kokoro` package) wrapper; returns audio plus chapter offsets |
| `audio.py` | ffmpeg MP3 encode with tags and chapters |
| `feed.py` | episodes.json to RSS 2.0 with iTunes tags (feedgen) |
| `storage.py` | R2 over the S3 API (boto3) |
| `pipeline.py` | Episode directories, the stages for one episode, publishing |
| `work.py` | Renders due episodes; retries with backoff; recovers crashed runs |
| `inbox.py` | Drop-folder intake |
| `remote.py` | Claims jobs from the Worker queue and reports their status |
| `cli.py` | `devoncast init / add / list / remove / rebuild-feed / voices` |
| `mcp_server.py` | FastMCP stdio server for Claude Desktop and Claude Code |

## Key decisions

**TTS: Kokoro-82M.** Small, fast on Apple Silicon, and stable over 20+ minutes of text, which
matters more for this use than expressiveness in short clips. Larger expressive models (Dia,
Orpheus, Sesame CSM, Chatterbox) tend to drift or hallucinate over long-form input. The engine
sits behind one function (`synthesize(chapters, voice, speed)`), so another model can be A/B
tested later. Default voice `af_heart`.

**LLM on the subscription.** Two paths, neither using an API key:
- CLI path: `script.py` runs `claude -p` (Claude Code headless, logged in with the subscription)
  with tools and MCP servers disabled.
- MCP path: Claude writes the speech script in the conversation and passes the finished script,
  title and notes to the `add_episode` tool, which makes no LLM calls.
- Fallback when neither is available: the deterministic prep output is read as-is.

**Feed state.** `episodes.json` in R2 is the source of truth. `feed.xml` is always regenerated
from it, never edited in place. Each episode has a stable GUID, enclosure byte length, and
duration.

**Hosting.** Dedicated R2 bucket `devoncast` served at `https://devoncast.polyculture.net`.
Everything lives under a random path prefix (`/<secret>/feed.xml`, `/<secret>/episodes/...`).
R2 public domains don't allow listing, so only someone who has the feed URL can find
anything. The feed sets `<itunes:block>yes</itunes:block>` so directories don't index it. This is
privacy by obscurity, a deliberate trade-off, because podcast apps handle authenticated feeds
inconsistently. `feed.xml` and `episodes.json` get `Cache-Control: max-age=60`; MP3s are
immutable and cached for a long time.

**Credentials.** A bucket-scoped R2 API token (Object Read & Write on `devoncast` only), stored in
the macOS Keychain (service `devoncast`). Never committed.

**Long MCP calls.** TTS for a 20-minute episode takes longer than an MCP call should. The MCP
tools create a queued episode, start a detached `devoncast work` process, and return right away.
`episode_status` reports progress.

## Interfaces

```
devoncast init                              # write config, cover art, empty feed
devoncast add <path|url> [--voice af_heart] [--no-llm] [--no-publish]
devoncast redo <ep> [--from STAGE]          # discard a stage's output and rerun from there
devoncast retry <ep>                        # retry a failed episode now
devoncast work                              # render everything due
devoncast status [<ep>]
devoncast list
devoncast remove <guid-or-slug>
devoncast rebuild-feed
devoncast voices <text>                     # render a sample per voice to compare
devoncast mcp                               # run the MCP server (stdio)
```

Local MCP tools: `add_episode(title, script, summary, show_notes, source_url?, voice?)`,
`add_document(path_or_url, voice?)`, `episode_status(id)`, `retry_episode(id)`, `list_episodes()`,
`remove_episode(id)`, `feed_url()`.

## Steps 4 and 5 as built

- **Inbox:** a launchd agent with `WatchPaths` on `~/devoncast/Inbox`, plus a 5-minute interval
  so retries come due, runs `devoncast inbox`. A file stays in `processing/` while its episode
  renders or waits to retry, then moves to `done/` or `failed/` (and back to `done/` if a manual
  retry succeeds). Files left in `processing/` with no episode are picked up again.
  The Finder Quick Action (an Automator service generated by `devoncast install`) copies the
  selected files into the Inbox, so every local path goes through one intake.
- **Remote MCP:** Cloudflare Worker `devoncast-mcp` using `createMcpHandler` (stateless, MCP SDK
  v2). Tools: `add_episode`, `add_url`, `job_status`, `list_jobs`, `retry_job`, `list_episodes`,
  `feed_url`. There is deliberately no remote delete. Jobs go into the D1 table `jobs`.
- **Queue protocol:** `POST /api/jobs/claim` (bearer token) atomically claims the oldest queued
  job. The Mac turns it into a local episode and confirms within seconds by reporting
  `accepted` with the episode id. From then on the Mac owns the job: it retries failures itself
  and reports `rendering`, `retrying`, `published` or `failed` via `POST /api/jobs/<id>`. Only an
  unconfirmed claim (the response was lost) is handed out again, after 10 minutes, and after 3
  claims it fails. A job handed out again finds its existing episode by job id, so it never
  renders twice. Reports are idempotent: each episode keeps its last report in `remote.json`, and
  every poll resends anything that didn't get through. A launchd agent runs `devoncast poll`
  every 60 seconds.
- **Auth:** the MCP endpoint lives at `/<MCP_PATH_SECRET>/mcp`, so the URL is the credential,
  like the feed. The upgrade path is OAuth through Cloudflare Access in front of the Worker.
- **Serialization:** a file lock (`~/devoncast/.render.lock`) lets only one episode render at a
  time across the Inbox, the poller, the local MCP and the CLI. A second lock
  (`.catalog.lock`) guards the read-modify-write of `episodes.json`, so removing an episode doesn't
  wait for a render. Publishing writes `feed.xml` before `episodes.json` and deletes replaced
  audio only after both, so a failed upload never leaves the feed pointing at a missing file.
- **Installed copy:** background agents and Claude Desktop run `~/.local/bin/devoncast`
  (`uv tool install`), because launchd jobs can't read `~/Documents`.

## Build order

1. Core pipeline and CLI, tested end to end on a real document
2. R2 publishing and the feed
3. Local MCP server for Claude Desktop
4. Finder Quick Action (Shortcuts) and a launchd-watched drop folder
5. Remote MCP on a Cloudflare Worker plus a job queue, so claude.ai on web and mobile can add
   episodes. The Mac polls the queue and does the TTS.
