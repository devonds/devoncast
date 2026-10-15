"""Stage 1: turn a file path or URL into markdown text."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import trafilatura

from .errors import PermanentError


@dataclass
class Source:
    markdown: str
    name: str  # filename stem or page title, used as a fallback episode title
    url: str | None = None


def is_url(target: str) -> bool:
    return target.startswith(("http://", "https://"))


def extract(target: str) -> Source:
    if is_url(target):
        return _from_url(target)
    path = Path(target).expanduser()
    if not path.is_file():
        raise PermanentError(f"No such file: {path}")
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown", ".txt", ""}:
        return Source(path.read_text(), path.stem)
    if suffix in {".html", ".htm"}:
        return Source(_html_to_markdown(path.read_text()), path.stem)
    if suffix in {".docx", ".rtf", ".odt", ".epub"}:
        return Source(_pandoc(path), path.stem)
    raise PermanentError(f"Unsupported file type: {suffix}")


def _from_url(url: str) -> Source:
    html = trafilatura.fetch_url(url)
    if not html:
        raise RuntimeError(f"Could not fetch {url}")
    meta = trafilatura.extract_metadata(html)
    title = meta.title if meta and meta.title else url
    body = _html_to_markdown(html)
    return Source(f"# {title}\n\n{body}", title, url)


def _html_to_markdown(html: str) -> str:
    text = trafilatura.extract(html, output_format="markdown", include_links=False, include_images=False)
    if not text:
        raise PermanentError("No readable article text found")
    return text


def _pandoc(path: Path) -> str:
    try:
        out = subprocess.run(
            ["pandoc", str(path), "-t", "gfm", "--wrap=none"], capture_output=True, text=True, check=True
        )
    except FileNotFoundError:
        raise PermanentError(f"Converting {path.suffix} needs pandoc: brew install pandoc") from None
    return out.stdout
