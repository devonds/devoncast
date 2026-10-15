import json

import pytest

from devoncast import inbox, pipeline, remote, work
from devoncast.errors import PermanentError
from devoncast.extract import Source


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(inbox, "_settled", lambda path: True)


def drop(cfg, name, text):
    folder = inbox.inbox_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text)


def files(cfg, sub):
    return sorted(p.name for p in (inbox.inbox_dir(cfg) / sub).iterdir())


# --- Inbox ----------------------------------------------------------------------------------


def test_dropping_a_revised_file_with_the_same_name_makes_a_new_episode(env, store, no_sleep):
    drop(env, "notes.md", "# First\n\nOne.")
    inbox.process(env)
    drop(env, "notes.md", "# Second\n\nTwo.")
    inbox.process(env)

    titles = [e["title"] for e in json.loads(store.objects["episodes.json"])]
    assert titles == ["First", "Second"]
    done = files(env, "done")
    assert len(done) == 2 and "notes.md" in done


def test_inbox_file_waits_in_processing_while_retrying(env, fake_tts, no_sleep):
    fake_tts.failures = 1
    drop(env, "brief.md", "# Brief\n\nText.")
    inbox.process(env)
    assert files(env, "processing") == ["brief.md"]

    [d] = env.episodes_dir.iterdir()
    st = pipeline.status(d)
    st["retry_at"] = 0
    (d / "status.json").write_text(json.dumps(st))
    inbox.process(env)  # the periodic launchd run
    assert files(env, "processing") == []
    assert files(env, "done") == ["brief.md"]


def test_inbox_file_moves_to_failed_on_a_permanent_failure_and_back_on_retry(env, no_sleep, monkeypatch):
    def unreadable(target):
        raise PermanentError("No readable article text found")

    monkeypatch.setattr(pipeline.extract, "extract", unreadable)
    drop(env, "page.html", "<p>x</p>")
    inbox.process(env)
    assert files(env, "failed") == ["page.html"]

    monkeypatch.setattr(pipeline.extract, "extract", lambda target: Source("# Page\n\nText.", "page"))
    [d] = env.episodes_dir.iterdir()
    work.requeue(d)
    work.work(env)
    assert files(env, "failed") == []
    assert files(env, "done") == ["page.html"]


def test_orphan_in_processing_is_picked_up(env, no_sleep):
    folder = inbox.inbox_dir(env) / "processing"
    folder.mkdir(parents=True)
    (folder / "lost.md").write_text("# Lost\n\nText.")
    inbox.process(env)
    assert files(env, "done") == ["lost.md"]


# --- Remote queue ---------------------------------------------------------------------------


class FakeApi:
    def __init__(self, jobs=()):
        self.jobs = list(jobs)
        self.updates: list[tuple[str, dict]] = []
        self.down = False

    def claim(self):
        return self.jobs.pop(0) if self.jobs else None

    def update(self, job_id, **fields):
        if self.down:
            raise ConnectionError("offline")
        self.updates.append((job_id, fields))

    def statuses(self, job_id):
        return [f["status"] for j, f in self.updates if j == job_id]


@pytest.fixture
def api(env, monkeypatch):
    a = FakeApi()
    monkeypatch.setattr(remote, "Api", lambda cfg: a)
    return a


def script_job(job_id="j1", **extra):
    return {"id": job_id, "kind": "script", "episode_id": None,
            "payload": {"title": "Remote", "script": "# Intro\n\nHello.", "summary": "s"}, **extra}


def test_remote_job_reports_each_status_once(env, api):
    api.jobs = [script_job()]
    remote.poll(env)
    assert api.statuses("j1") == ["accepted", "rendering", "published"]
    remote.poll(env)  # nothing changed, nothing resent
    assert api.statuses("j1") == ["accepted", "rendering", "published"]


def test_lost_reports_are_resent_on_the_next_poll(env, api):
    api.jobs = [script_job()]
    api.down = True
    remote.poll(env)  # renders, but no report gets through
    api.down = False
    remote.poll(env)
    assert api.statuses("j1") == ["published"]


def test_job_handed_out_again_does_not_render_twice(env, api, fake_tts):
    api.jobs = [script_job()]
    api.down = True  # the "accepted" report is lost, so the Worker hands the job out again
    remote.poll(env)
    api.down = False
    api.jobs = [script_job()]
    remote.poll(env)
    assert fake_tts.calls == 1
    assert len(list(env.episodes_dir.iterdir())) == 1


def test_retry_job_resumes_the_failed_episode(env, api, fake_tts):
    fake_tts.failures = 100
    api.jobs = [script_job()]
    remote.poll(env)
    [d] = env.episodes_dir.iterdir()
    pipeline.set_status(d, "failed", error="TTS crashed", attempts=work.MAX_ATTEMPTS)
    remote.sync(env, api)
    assert api.statuses("j1")[-1] == "failed"

    fake_tts.failures = 0  # the user fixed it and called retry_job
    api.jobs = [script_job(status="queued", episode_id=d.name)]
    remote.poll(env)
    assert api.statuses("j1")[-1] == "published"
    assert len(list(env.episodes_dir.iterdir())) == 1


def test_bad_job_fails_without_stopping_the_queue(env, api):
    api.jobs = [{"id": "bad", "kind": "video", "episode_id": None, "payload": {}}, script_job("ok")]
    remote.poll(env)
    assert api.statuses("bad") == ["failed"]
    assert api.statuses("ok")[-1] == "published"
