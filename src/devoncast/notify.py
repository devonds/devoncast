"""macOS notifications."""

from __future__ import annotations

import subprocess


def notify(title: str, message: str) -> None:
    script = f"display notification {_applescript_str(message)} with title {_applescript_str(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True)


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"')[:200] + '"'
