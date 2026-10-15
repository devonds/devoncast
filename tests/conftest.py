"""Fixtures that run the real pipeline with TTS, encoding, R2 and notifications faked."""

from __future__ import annotations

import numpy as np
import pytest

from devoncast import audio, inbox, pipeline, tts, work
from devoncast.config import Config


class FakeStore:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.fail_puts: set[str] = set()  # names whose next put raises

    def url(self, name):
        return f"https://example.test/p/{name}"

    def put_file(self, name, path, content_type, cache=None):
        return self.put_bytes(name, path.read_bytes(), content_type)

    def put_bytes(self, name, data, content_type, cache=None):
        if name in self.fail_puts:
            self.fail_puts.discard(name)
            raise ConnectionError(f"upload of {name} failed")
        self.objects[name] = data
        return self.url(name)

    def get_bytes(self, name):
        return self.objects.get(name)

    def delete(self, name):
        self.objects.pop(name, None)


class FakeTTS:
    def __init__(self):
        self.calls = 0
        self.failures = 0  # how many upcoming calls raise

    def render(self, chapters, voice, speed, progress=None):
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("TTS crashed")
        return tts.Rendered(np.zeros(tts.SAMPLE_RATE, dtype=np.float32), [(0.0, chapters[0].title)])


@pytest.fixture
def cfg(tmp_path):
    return Config(home=tmp_path / "home", podcast={"title": "Test"}, tts={}, llm={"backend": "none"},
                  r2={"public_base_url": "https://example.test", "prefix": "p", "bucket": "b"}, remote={})


@pytest.fixture
def store(monkeypatch):
    s = FakeStore()
    monkeypatch.setattr(pipeline, "Store", lambda cfg: s)
    return s


@pytest.fixture
def fake_tts(monkeypatch):
    t = FakeTTS()
    monkeypatch.setattr(tts, "render", t.render)
    # The MP3 holds the title, so tests can tell which document an episode was made from.
    monkeypatch.setattr(audio, "encode_mp3", lambda rendered, out, title, *a, **k: out.write_text(title))
    return t


@pytest.fixture
def notes(monkeypatch):
    sent: list[tuple[str, str]] = []
    record = lambda title, message: sent.append((title, message))  # noqa: E731
    monkeypatch.setattr(work, "notify", record)
    monkeypatch.setattr(inbox, "notify", record)
    return sent


@pytest.fixture
def env(cfg, store, fake_tts, notes):
    return cfg

