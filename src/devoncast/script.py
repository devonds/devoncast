"""Stage 3: rewrite cleaned text for listening, and write title, summary and show notes.

Runs `claude -p` so the work is billed to the Claude subscription, not the API.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass

SCRIPT_GUIDE = """\
Rewrite the document as a script to be read aloud by a text-to-speech engine, for one listener
on a walk or in the car. Rules:

- Keep all the substance, in the original order. This is a faithful audio edition, not a summary.
  Cut only what makes no sense in audio: URLs, file paths, code, figure references, "see above",
  and administrative bits like scheduling logistics, video call links, or contact details.
- Start each section with a line "# <section title>" on its own (a chapter marker; it is not
  spoken), then a short spoken lead-in such as "Section two: modeling approach." so the listener
  hears where they are. Use one chapter per top-level section; fold small subsections into
  their parent and introduce them in speech instead.
- Open with one or two sentences saying what this is and what it covers. End with a brief sign-off.
- Turn bullet lists, tables, and fragments into full spoken sentences. Say "first", "second",
  "and finally" where order matters.
- Write numbers, symbols and abbreviations as they should be spoken: "about three hundred",
  "thirty percent", "versus", "for example". Write acronyms that are said letter by letter in
  plain capitals with no spaces or periods (AI, CRM, ROC AUC, DBT); the speech engine spells
  them out. Never write single letters separated by spaces. Keep words said as words ("SaaS").
  Prefer the expanded form on first use.
- Short sentences. No parentheses, no emoji, no markdown other than the "# " chapter lines.
- Address the listener as "you" if the document does.
"""

PROMPT = f"""{SCRIPT_GUIDE}
Also write episode metadata. Reply with exactly these four tagged blocks and nothing else:

<title>Specific episode title, under 70 characters</title>
<summary>One or two sentences for the episode list.</summary>
<show_notes>Markdown show notes: a short paragraph, then a bullet list of the chapters with one line each.</show_notes>
<script>
The full script.
</script>

The document follows.
"""


@dataclass
class Script:
    title: str
    summary: str
    show_notes: str
    script: str

    def to_dict(self) -> dict:
        return asdict(self)


def claude_available() -> bool:
    return shutil.which("claude") is not None


def write_script(cleaned: str, model: str = "") -> Script:
    cmd = [
        "claude", "-p", PROMPT,
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    if model:
        cmd += ["--model", model]
    # Run outside any project so no CLAUDE.md or project settings leak into the prompt.
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(cmd, input=cleaned, capture_output=True, text=True, cwd=tmp, timeout=900)
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    return parse_reply(proc.stdout)


def parse_reply(reply: str) -> Script:
    def tag(name: str) -> str:
        m = re.search(rf"<{name}>\s*(.*?)\s*</{name}>", reply, re.S)
        if not m:
            raise RuntimeError(f"LLM reply missing <{name}> block:\n{reply[:500]}")
        return m.group(1).strip()

    return Script(tag("title"), tag("summary"), tag("show_notes"), tag("script"))
