"""Renders every episode that is queued, due for a retry, or was cut off by a crash.

Intakes (the Inbox, the remote queue, local MCP) only create episode directories; `work` renders
them one at a time. A failed attempt is retried after a growing delay, resuming from the last
finished stage, so retrying a failed upload doesn't redo the TTS. The delays add up to about
nine hours, long enough to outlast a Claude usage limit.
"""

from __future__ import annotations

import time
import traceback
from datetime import datetime
from pathlib import Path

from . import pipeline
from .config import Config
from .errors import PermanentError
from .notify import notify

RETRY_DELAYS = [120, 600, 1800, 7200, 21600]  # seconds to wait after attempts 1 to 5
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1


def due(cfg: Config, now: float | None = None) -> list[Path]:
    """Episodes ready to run, oldest first. While the caller holds the render lock, an episode in
    a running stage can only be left over from a run that died, so it counts as due."""
    now = time.time() if now is None else now
    if not cfg.episodes_dir.exists():
        return []
    ready = []
    for d in sorted(cfg.episodes_dir.iterdir()):
        st = pipeline.status(d)
        stage = st.get("stage")
        retry_due = stage == "failed" and "retry_at" in st and st["retry_at"] <= now
        if stage == "queued" or stage in pipeline.ACTIVE or retry_due:
            ready.append(d)
    return ready


def work(cfg: Config, log=print) -> int:
    """Run every due episode and return how many were attempted. Returns at once if another
    process holds the render lock; that process looks for new episodes again before it exits."""
    attempted = 0
    while True:
        with pipeline.render_lock(cfg, wait=False) as got:
            if not got:
                return attempted
            while ready := due(cfg):
                for d in ready:
                    attempt(cfg, d, log)
                    attempted += 1
        # An episode created after the last check but before the lock was released found the
        # lock taken and left it to us.
        if not due(cfg):
            return attempted


def requeue(d: Path) -> None:
    """Make a failed or finished episode due again with a fresh set of attempts."""
    pipeline.set_status(d, "queued", attempts=0)


def attempt(cfg: Config, d: Path, log=print) -> None:
    """One try at rendering `d`. Call while holding the render lock."""
    st = pipeline.status(d)
    tries = st.get("attempts", 0) + 1
    with open(d / "log.txt", "a") as f:
        def elog(msg: str) -> None:
            log(f"{d.name}: {msg}")
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
            f.flush()

        if tries > MAX_ATTEMPTS:  # the earlier attempts died without recording a failure
            pipeline.set_status(d, "failed", error=st.get("error") or f"gave up after {MAX_ATTEMPTS} attempts")
            elog(f"giving up after {MAX_ATTEMPTS} attempts")
            _finished(cfg, d)
            return

        pipeline.set_status(d, "starting", attempts=tries)
        elog(f"attempt {tries} of {MAX_ATTEMPTS}")
        _changed(cfg, d)
        try:
            pipeline.run_locked(cfg, d, elog)
        except Exception as e:  # noqa: BLE001 - every failure is recorded and maybe retried
            f.write(traceback.format_exc())
            if isinstance(e, PermanentError) or tries >= MAX_ATTEMPTS:
                pipeline.set_status(d, "failed", error=str(e))
                elog(f"failed: {e}")
            else:
                retry_at = time.time() + RETRY_DELAYS[tries - 1]
                pipeline.set_status(d, "failed", error=str(e), retry_at=retry_at)
                elog(f"failed: {e}; retrying at {datetime.fromtimestamp(retry_at):%H:%M}")
        _finished(cfg, d)


def describe(d: Path) -> str:
    """One line on where an episode stands."""
    st = pipeline.status(d)
    stage = st.get("stage", "unknown")
    if stage == "failed" and st.get("retry_at"):
        when = datetime.fromtimestamp(st["retry_at"])
        return f"retrying at {when:%H:%M} (attempt {st.get('attempts', 0)} failed: {st.get('error')})"
    if stage == "failed":
        return f"failed: {st.get('error')}"
    if stage == "synthesize" and st.get("total"):
        return f"synthesizing: {st['done']} of {st['total']} paragraphs"
    return stage


def _finished(cfg: Config, d: Path) -> None:
    st = pipeline.status(d)
    name = pipeline.request(d).get("name") or d.name
    if st["stage"] in pipeline.FINISHED:
        title = pipeline.read_json(d / "meta.json").get("title", name)
        notify("New Devoncast episode", title)
    elif st.get("retry_at"):
        notify("Devoncast will retry", f"{name}: {describe(d)}")
    else:
        notify("Devoncast failed", f"{name}: {st.get('error')}")
    _changed(cfg, d)


def _changed(cfg: Config, d: Path) -> None:
    """Tell whoever asked for this episode that its status changed."""
    kind = pipeline.request(d).get("origin", {}).get("kind")
    try:
        if kind == "inbox":
            from . import inbox

            inbox.episode_changed(cfg, d)
        elif kind == "remote":
            from . import remote

            remote.report(cfg, d)
    except Exception as e:  # noqa: BLE001 - reporting must not fail the render; it is retried later
        print(f"{d.name}: could not report status: {e}")
