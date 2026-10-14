"""Stage 4a: Kokoro text-to-speech. Returns 24 kHz mono audio plus chapter start times."""

from __future__ import annotations

import os
import warnings
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from .prep import Chapter

SAMPLE_RATE = 24_000
PARAGRAPH_PAUSE = 0.45
CHAPTER_PAUSE = 1.2

VOICES = ["af_heart", "af_bella", "af_nicole", "am_michael", "am_fenrir", "bf_emma", "bm_george"]

_pipelines: dict[str, object] = {}


@dataclass
class Rendered:
    audio: np.ndarray
    chapter_starts: list[tuple[float, str]]  # (seconds, title)

    @property
    def duration(self) -> float:
        return len(self.audio) / SAMPLE_RATE


def _pipeline(voice: str):
    lang = voice[0]  # Kokoro voices are prefixed by language: a = American, b = British
    if lang not in _pipelines:
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        warnings.filterwarnings("ignore", module="torch")
        from kokoro import KPipeline

        _pipelines[lang] = KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M")
    return _pipelines[lang]


def speak(text: str, voice: str = "af_heart", speed: float = 1.0) -> np.ndarray:
    pipe = _pipeline(voice)
    parts = [a.numpy() for _, _, a in pipe(text, voice=voice, speed=speed, split_pattern=None) if a is not None]
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def render(
    chapters: list[Chapter],
    voice: str = "af_heart",
    speed: float = 1.0,
    progress: Callable[[int, int], None] | None = None,
) -> Rendered:
    total = sum(len(c.paragraphs) for c in chapters)
    done = 0
    pieces: list[np.ndarray] = []
    starts: list[tuple[float, str]] = []
    cursor = 0
    for ci, chapter in enumerate(chapters):
        if ci:
            pieces.append(_silence(CHAPTER_PAUSE - PARAGRAPH_PAUSE))
            cursor += len(pieces[-1])
        starts.append((cursor / SAMPLE_RATE, chapter.title))
        for para in chapter.paragraphs:
            for chunk in (speak(para, voice, speed), _silence(PARAGRAPH_PAUSE)):
                pieces.append(chunk)
                cursor += len(chunk)
            done += 1
            if progress:
                progress(done, total)
    return Rendered(np.concatenate(pieces).astype(np.float32), starts)


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)
