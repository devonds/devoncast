# devoncast

Turns documents into spoken MP3s with local Kokoro TTS and publishes them to a private podcast
feed on Cloudflare R2. Design: [docs/design.md](docs/design.md).

## Setup

```sh
brew install ffmpeg          # pandoc too, if you want .docx input
uv sync
uv run devoncast init        # writes ~/.config/devoncast/config.toml
```

Create a bucket-scoped R2 token (Cloudflare dashboard → R2 → Manage API tokens → Create Account
API token → Object Read & Write, applied to the `devoncast` bucket only) and store it in the
Keychain (each command prompts for the value):

```sh
security add-generic-password -s devoncast -a r2_access_key_id -w
security add-generic-password -s devoncast -a r2_secret_access_key -w
```

Then run `uv run devoncast init` again.
It uploads the cover art and an empty feed and prints the feed URL to subscribe to.

The rewrite step uses `claude -p`, so Claude Code must be installed and logged in with your Claude
subscription. Without it, or with `--no-llm`, the cleaned text is read as-is.

## Use

```sh
devoncast add ~/Downloads/some-brief.md        # or .txt, .html, .docx, or a URL
devoncast add https://example.com/article --voice am_michael
devoncast redo <episode> --from synthesize      # rerender with a different voice, keep the script
devoncast redo <episode> --from script          # rewrite the script too
devoncast retry <episode>                       # retry a failed episode now, resuming where it stopped
devoncast list | remove <episode> | feed-url | status
devoncast voices                                # write samples of each voice to compare
```

Each episode's working files (request, status, log, source, cleaned text, script, MP3) are in
`~/devoncast/episodes/<date>-<slug>/`. To fix wording, edit `script.txt` there and run
`devoncast redo <episode>`.

Failures in the background (Inbox, remote queue, MCP) retry on their own after 2 min, 10 min,
30 min, 2 h and 6 h, resuming from the stage that failed. Unsupported files and pages with no
readable text fail at once. `devoncast status` shows where each episode stands, and the
episode's `log.txt` has the details.

## Installed copy and background agents

The background pieces run the copy installed with `uv tool install`, because launchd jobs can't
read `~/Documents`. After changing code, reinstall:

```sh
uv tool install --reinstall --python 3.12 .
devoncast install        # first time only: Inbox, launchd agents, Quick Action
```

`devoncast install` sets up:

- `~/devoncast/Inbox`: drop a file (or a URL dragged from a browser, which makes a `.webloc`)
  and it becomes an episode. Files move to `processing/` (where they also wait between retries),
  then `done/` or `failed/`. Drag the
  folder to the Dock or Finder sidebar to get a drop target.
- Finder Quick Action **Add to Devoncast** (right-click a file → Quick Actions). It copies the
  file into the Inbox.
- A poller that checks the remote queue (below) every minute.

A notification appears when an episode is published, will be retried, or fails for good. Logs are in `~/devoncast/logs/`.
Only one episode renders at a time; the rest wait.

## Claude Desktop (local MCP)

Registered in `~/Library/Application Support/Claude/claude_desktop_config.json` as `devoncast`
(runs `~/.local/bin/devoncast mcp`). Restart Claude Desktop, then say "add that artifact to my
devoncast feed" or "add https://… to devoncast". For Claude Code:

```sh
claude mcp add devoncast -s user -- ~/.local/bin/devoncast mcp
```

## claude.ai web and mobile (remote MCP)

`worker/` is a Cloudflare Worker at `devoncast-mcp.polyculture.net` serving an MCP server for
claude.ai custom connectors. Its tools queue jobs in D1; the Mac's poller claims and renders them,
so episodes only render while the Mac is awake.

The connector URL contains a secret path, which acts as the credential:

```sh
echo "https://devoncast-mcp.polyculture.net/$(security find-generic-password -s devoncast -a mcp_path_secret -w)/mcp"
```

Add it in claude.ai → Settings → Connectors → Add custom connector. Deploy changes with
`cd worker && npm run deploy`. Worker secrets: `MCP_PATH_SECRET`, `WORKER_TOKEN` (the Mac holds the
same token as the `worker_token` Keychain item), `FEED_PREFIX`.
