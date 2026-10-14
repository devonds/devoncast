"""Stage 2: deterministic markdown cleanup, and the script format shared by later stages.

Script format: plain paragraphs separated by blank lines. A line starting with "# " starts a
chapter and is not read as its own paragraph unless the speaker text repeats it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from markdown_it import MarkdownIt
from markdown_it.token import Token

_md = MarkdownIt("commonmark").enable("table").enable("strikethrough")


def clean_markdown(text: str) -> str:
    """Reduce markdown to headings and plain paragraphs. Tables become one sentence per row."""
    tokens = _md.parse(_strip_front_matter(text))
    out: list[str] = []
    i = 0
    list_depth = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "heading_open":
            out.append(f"# {_inline_text(tokens[i + 1])}")
            i += 3
            continue
        if tok.type in ("bullet_list_open", "ordered_list_open"):
            list_depth += 1
        elif tok.type in ("bullet_list_close", "ordered_list_close"):
            list_depth -= 1
        elif tok.type == "inline":
            line = _inline_text(tok)
            if line:
                out.append(_end_sentence(line) if list_depth else line)
        elif tok.type == "table_open":
            j = _skip_to(tokens, i, "table_close")
            out.extend(_table_sentences(tokens[i : j + 1]))
            i = j
        elif tok.type in ("fence", "code_block", "html_block"):
            pass  # code and raw HTML don't survive being read aloud
        i += 1
    return "\n\n".join(out).strip() + "\n"


def _strip_front_matter(text: str) -> str:
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)


def _inline_text(tok: Token) -> str:
    parts: list[str] = []
    for child in tok.children or []:
        if child.type in ("text", "code_inline"):
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append(" ")
        # images, html_inline and link markup are dropped; link text arrives as text children
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _end_sentence(line: str) -> str:
    return line if line[-1] in ".!?:;" else line + "."


def _skip_to(tokens: list[Token], i: int, kind: str) -> int:
    while tokens[i].type != kind:
        i += 1
    return i


def _table_sentences(tokens: list[Token]) -> list[str]:
    rows: list[list[str]] = []
    for tok in tokens:
        if tok.type == "tr_open":
            rows.append([])
        elif tok.type == "inline":
            rows[-1].append(_inline_text(tok))
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    sentences = []
    for row in body:
        pairs = [f"{h}: {v}" if h else v for h, v in zip(header, row) if v]
        sentences.append(_end_sentence("; ".join(pairs)))
    return sentences


@dataclass
class Chapter:
    title: str
    paragraphs: list[str] = field(default_factory=list)


def parse_script(script: str) -> list[Chapter]:
    """Split a script into chapters. Text before the first heading goes into an untitled intro."""
    chapters = [Chapter("Introduction")]
    for block in re.split(r"\n\s*\n", script.strip()):
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        while lines and lines[0].startswith("# "):
            title = lines.pop(0)[2:].strip()
            if chapters[-1].paragraphs or chapters[-1].title != "Introduction":
                chapters.append(Chapter(title))
            else:
                chapters[-1].title = title
        rest = " ".join(l.strip() for l in lines).strip()
        if rest:
            chapters[-1].paragraphs.append(rest)
    return [c for c in chapters if c.paragraphs]


def fallback_script(cleaned: str) -> str:
    """Script used when no LLM is available: headings are also spoken, as section intros."""
    out = []
    for block in cleaned.split("\n\n"):
        if block.startswith("# "):
            title = block[2:].strip()
            out.append(f"# {title}\n{_end_sentence(title)}")
        else:
            out.append(block)
    return "\n\n".join(out)
