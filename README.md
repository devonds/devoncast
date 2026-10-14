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
API token → Object Read & Write, applied to the `devoncast` bucket only). Put its Access Key ID and
Secret Access Key in `~/.config/devoncast/config.toml`, then run `uv run devoncast init` again.
It uploads the cover art and an empty feed and prints the feed URL to subscribe to.

The rewrite step uses `claude -p`, so Claude Code must be installed and logged in with your Claude
subscription. Without it, or with `--no-llm`, the cleaned text is read as-is.

## Use

```sh
devoncast add ~/Downloads/some-brief.md        # or .txt, .html, .docx, or a URL
devoncast add https://example.com/article --voice am_michael
devoncast redo <episode> --from synthesize      # rerender with a different voice, keep the script
devoncast redo <episode> --from script          # rewrite the script too
devoncast list | remove <episode> | feed-url | status
devoncast voices                                # write samples of each voice to compare
```

Each episode's working files (source, cleaned text, script, MP3) are in
`~/devoncast/episodes/<date>-<slug>/`. To fix wording, edit `script.txt` there and run
`devoncast redo <episode>`.

## Claude Desktop

The MCP server is registered in `~/Library/Application Support/Claude/claude_desktop_config.json`
as `devoncast` (runs `uv run devoncast mcp`). Restart Claude Desktop, then say things like
"add that artifact to my devoncast feed" or "add https://… to devoncast". For Claude Code:

```sh
claude mcp add devoncast -s user -- uv --directory ~/Documents/code/devoncast run --quiet devoncast mcp
```
