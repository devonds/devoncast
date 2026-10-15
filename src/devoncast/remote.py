"""Pulls jobs queued through the remote MCP server (Cloudflare Worker) and renders them here.

launchd runs `devoncast poll` every minute. Each run claims every queued job and turns it into a
local episode directory, reports the episode id back (after which the Worker never hands the job
out again), then renders whatever is due. Status reports are idempotent: each episode remembers
what it last reported in remote.json, and every poll resends anything that changed but didn't
get through.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from pathlib import Path

from . import pipeline, work
from .config import Config, keychain
from .errors import PermanentError


class Api:
    def __init__(self, cfg: Config):
        self.base = cfg.remote.get("api_url", "").rstrip("/")
        self.token = cfg.remote.get("worker_token") or keychain("worker_token")
        if not (self.base and self.token):
            raise SystemExit("Remote queue not configured: set [remote] api_url and the worker_token Keychain item")

    def _post(self, path: str, body: dict | None = None) -> dict | None:
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(body or {}).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                     "User-Agent": "devoncast-poller"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()) if resp.status == 200 else None

    def claim(self) -> dict | None:
        return self._post("/api/jobs/claim")

    def update(self, job_id: str, **fields) -> None:
        self._post(f"/api/jobs/{job_id}", fields)


def poll(cfg: Config, log=print) -> int:
    api = Api(cfg)
    sync(cfg, api)
    claimed = 0
    while job := api.claim():
        claimed += 1
        log(f"job {job['id']}: {job['kind']}")
        accept(cfg, api, job)
    work.work(cfg, log)
    return claimed


def accept(cfg: Config, api: Api, job: dict) -> None:
    """Turn a claimed job into a local episode, or pick up the one it already has."""
    d = _existing(cfg, job["id"])
    if d:
        # Handed out again: a retry_job after a failure, or the first report didn't get through.
        if pipeline.status(d).get("stage") == "failed":
            work.requeue(d)
        (d / "remote.json").unlink(missing_ok=True)
    else:
        try:
            d = _create(cfg, job)
        except Exception as e:  # noqa: BLE001 - a bad job must not stop the queue
            api.update(job["id"], status="failed", error=str(e)[:2000])
            return
    _try_report(cfg, d, api)


def _existing(cfg: Config, job_id: str) -> Path | None:
    for d in _remote_episodes(cfg):
        if pipeline.request(d)["origin"]["job_id"] == job_id:
            return d
    return None


def _remote_episodes(cfg: Config) -> list[Path]:
    if not cfg.episodes_dir.exists():
        return []
    return [d for d in sorted(cfg.episodes_dir.iterdir())
            if pipeline.request(d).get("origin", {}).get("kind") == "remote"]


def _create(cfg: Config, job: dict) -> Path:
    p = job["payload"]
    origin = {"kind": "remote", "job_id": job["id"]}
    if job["kind"] == "script":
        return pipeline.write_script_episode(cfg, p["title"], p["script"], p["summary"], p.get("show_notes", ""),
                                             p.get("source_url"), voice=p.get("voice"), origin=origin)
    if job["kind"] == "url":
        return pipeline.create_episode(cfg, p["url"].split("://", 1)[-1], target=p["url"],
                                       voice=p.get("voice"), origin=origin)
    raise PermanentError(f"unknown job kind {job['kind']}")


def remote_status(d: Path) -> dict:
    """The job fields that describe the episode's current state."""
    st = pipeline.status(d)
    stage = st.get("stage")
    fields = {"episode_id": d.name}
    if stage in pipeline.FINISHED:
        title = pipeline.read_json(d / "meta.json").get("title")
        return fields | {"status": "published", "title": title}
    if stage == "failed" and "retry_at" in st:
        return fields | {"status": "retrying", "error": work.describe(d)}
    if stage == "failed":
        return fields | {"status": "failed", "error": str(st.get("error"))[:2000]}
    if stage in pipeline.ACTIVE:
        return fields | {"status": "rendering"}
    return fields | {"status": "accepted"}


def report(cfg: Config, d: Path, api: Api | None = None) -> None:
    """Send the episode's status to the Worker if it changed since the last report."""
    job_id = pipeline.request(d)["origin"]["job_id"]
    fields = remote_status(d)
    if pipeline.read_json(d / "remote.json") == fields:
        return
    (api or Api(cfg)).update(job_id, **fields)
    (d / "remote.json").write_text(json.dumps(fields))


def sync(cfg: Config, api: Api) -> None:
    """Resend any status report that didn't get through."""
    for d in _remote_episodes(cfg):
        _try_report(cfg, d, api)


def _try_report(cfg: Config, d: Path, api: Api) -> None:
    try:
        report(cfg, d, api)
    except Exception as e:  # noqa: BLE001 - sync tries again on the next poll
        print(f"{datetime.now():%H:%M:%S} {d.name}: could not report status: {e}")
