import json

import pytest

from devoncast import feed, pipeline, work


def doc(tmp_path, name, heading):
    path = tmp_path / name
    path.write_text(f"# {heading}\n\nSome text about {heading}.\n")
    return path


def make_due(d):
    """Pretend the retry delay has passed."""
    st = pipeline.status(d)
    st["retry_at"] = 0
    (d / "status.json").write_text(json.dumps(st))


def catalog(store):
    return json.loads(store.objects["episodes.json"])


def test_same_name_twice_gets_separate_episodes(env, tmp_path, store):
    first = pipeline.create_episode(env, "notes", target=str(doc(tmp_path, "notes.md", "First draft")))
    second = pipeline.create_episode(env, "notes", target=str(doc(tmp_path, "notes.md", "Revised draft")))
    assert first != second
    assert second.name == first.name + "-2"

    work.work(env)

    assert [e["title"] for e in catalog(store)] == ["First draft", "Revised draft"]
    assert (second / "episode.mp3").read_text() == "Revised draft"


def test_input_file_is_copied_so_the_original_can_move(env, tmp_path):
    src = doc(tmp_path, "brief.md", "Brief")
    d = pipeline.create_episode(env, "brief", target=str(src))
    src.unlink()
    work.work(env)
    assert pipeline.status(d)["stage"] == "done"


def test_script_episodes_with_the_same_title_dont_overwrite(env, store):
    a = pipeline.write_script_episode(env, "Weekly update", "# Intro\n\nOne.", "s")
    b = pipeline.write_script_episode(env, "Weekly update", "# Intro\n\nTwo.", "s")
    assert (a / "script.txt").read_text() == "# Intro\n\nOne."
    work.work(env)
    assert len(catalog(store)) == 2
    assert pipeline.status(a)["stage"] == pipeline.status(b)["stage"] == "done"


def test_failed_attempt_retries_later_from_the_failed_stage(env, tmp_path, fake_tts, store, monkeypatch, notes):
    extracts = []
    real_extract = pipeline.extract.extract
    monkeypatch.setattr(pipeline.extract, "extract", lambda t: extracts.append(t) or real_extract(t))
    d = pipeline.create_episode(env, "doc", target=str(doc(tmp_path, "doc.md", "Doc")))
    fake_tts.failures = 1

    work.work(env)
    st = pipeline.status(d)
    assert st["stage"] == "failed" and st["attempts"] == 1 and st["retry_at"] > 0
    assert notes[-1][0] == "Devoncast will retry"

    work.work(env)  # not due yet
    assert fake_tts.calls == 1

    make_due(d)
    work.work(env)
    assert pipeline.status(d)["stage"] == "done"
    assert len(extracts) == 1  # resumed at synthesize; earlier stages were kept
    assert notes[-1] == ("New Devoncast episode", "Doc")


def test_permanent_failure_is_not_retried(env, tmp_path, notes):
    bad = tmp_path / "slides.key"
    bad.write_text("x")
    d = pipeline.create_episode(env, "slides", target=str(bad))
    work.work(env)
    st = pipeline.status(d)
    assert st["stage"] == "failed" and "retry_at" not in st
    assert "Unsupported file type" in st["error"]
    assert notes[-1][0] == "Devoncast failed"


def test_gives_up_after_max_attempts(env, tmp_path, fake_tts):
    d = pipeline.create_episode(env, "doc", target=str(doc(tmp_path, "doc.md", "Doc")))
    fake_tts.failures = 100
    for _ in range(work.MAX_ATTEMPTS):
        work.work(env)
        if "retry_at" in pipeline.status(d):
            make_due(d)
    st = pipeline.status(d)
    assert st["stage"] == "failed" and "retry_at" not in st
    assert fake_tts.calls == work.MAX_ATTEMPTS


def test_crashed_run_is_resumed(env, tmp_path, fake_tts):
    d = pipeline.create_episode(env, "doc", target=str(doc(tmp_path, "doc.md", "Doc")))
    pipeline.set_status(d, "synthesize", done=3, total=9)  # the process died mid-render
    work.work(env)
    assert pipeline.status(d)["stage"] == "done"


def test_run_that_dies_every_time_is_abandoned(env, tmp_path, fake_tts):
    d = pipeline.create_episode(env, "doc", target=str(doc(tmp_path, "doc.md", "Doc")))
    pipeline.set_status(d, "synthesize", attempts=work.MAX_ATTEMPTS)
    work.work(env)
    assert pipeline.status(d)["stage"] == "failed"
    assert fake_tts.calls == 0


def test_requeue_resets_attempts(env, tmp_path, fake_tts):
    d = pipeline.create_episode(env, "doc", target=str(doc(tmp_path, "doc.md", "Doc")))
    pipeline.set_status(d, "failed", attempts=work.MAX_ATTEMPTS, error="boom")
    work.requeue(d)
    work.work(env)
    assert pipeline.status(d)["stage"] == "done"


def test_work_skips_when_another_process_is_rendering(env, tmp_path):
    d = pipeline.create_episode(env, "doc", target=str(doc(tmp_path, "doc.md", "Doc")))
    with pipeline.render_lock(env):
        assert work.work(env) == 0
    assert pipeline.status(d)["stage"] == "queued"


def test_old_audio_is_kept_when_the_feed_update_fails(env, tmp_path, store):
    d = pipeline.create_episode(env, "doc", target=str(doc(tmp_path, "doc.md", "Doc")))
    work.work(env)
    old_audio = catalog(store)[0]["audio_name"]

    pipeline.reset_from(d, "synthesize")
    meta = json.loads((d / "meta.json").read_text())
    (d / "meta.json").write_text(json.dumps(meta | {"title": "Doc, corrected"}))
    store.fail_puts.add(feed.FEED)
    with pytest.raises(ConnectionError):
        pipeline.run(env, d)
    assert old_audio in store.objects  # the live feed still points at it

    pipeline.run(env, d)
    eps = catalog(store)
    assert len(eps) == 1 and eps[0]["audio_name"] != old_audio
    assert old_audio not in store.objects


def test_find_episode_dir_prefers_exact_name(env):
    a = pipeline.new_episode_dir(env, "notes")
    pipeline.new_episode_dir(env, "notes")
    assert pipeline.find_episode_dir(env, a.name) == a
