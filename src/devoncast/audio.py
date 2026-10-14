"""Stage 4b: encode rendered audio to MP3 with ID3 tags and chapter markers."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import soundfile as sf

from .tts import SAMPLE_RATE, Rendered


def encode_mp3(rendered: Rendered, out: Path, title: str, artist: str, album: str, bitrate: str = "64k") -> None:
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        meta = Path(tmp) / "meta.txt"
        sf.write(wav, rendered.audio, SAMPLE_RATE)
        meta.write_text(_ffmetadata(rendered, title, artist, album))
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(wav), "-i", str(meta), "-map_metadata", "1", "-map_chapters", "1",
                "-ac", "1", "-codec:a", "libmp3lame", "-b:a", bitrate,
                "-id3v2_version", "3", str(out),
            ],
            check=True,
        )


def _ffmetadata(rendered: Rendered, title: str, artist: str, album: str) -> str:
    lines = [";FFMETADATA1", f"title={_esc(title)}", f"artist={_esc(artist)}", f"album={_esc(album)}", "genre=Podcast"]
    ends = [s for s, _ in rendered.chapter_starts[1:]] + [rendered.duration]
    for (start, name), end in zip(rendered.chapter_starts, ends):
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}", f"END={int(end * 1000)}", f"title={_esc(name)}"]
    return "\n".join(lines) + "\n"


def _esc(value: str) -> str:
    for ch in "\\=;#\n":
        value = value.replace(ch, "\\" + ch)
    return value
