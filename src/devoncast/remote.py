"""Pulls jobs queued through the remote MCP server (Cloudflare Worker) and renders them here.

launchd runs `devoncast poll` every minute. Each run claims jobs until the queue is empty.
"""

from __future__ import annotations

import json
import urllib.request

from . import pipeline
from .config import Config, keychain
from .inbox import notify


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
    handled = 0
    while job := api.claim():
        handled += 1
        log(f"job {job['id']}: {job['kind']}")
        try:
            d, meta = _render(cfg, job, api, log)
        except Exception as e:  # noqa: BLE001 - report the failure to the queue, then continue
            api.update(job["id"], status="failed", error=str(e)[:2000])
            notify("Devoncast failed", f"Remote job: {e}")
            continue
        api.update(job["id"], status="published", episode_id=d.name, title=meta["title"])
        notify("New Devoncast episode", meta["title"])
    return handled


def _render(cfg: Config, job: dict, api: Api, log) -> tuple:
    p = job["payload"]
    voice = p.get("voice")
    if job["kind"] == "script":
        d = pipeline.write_script_episode(cfg, p["title"], p["script"], p["summary"],
                                          p.get("show_notes", ""), p.get("source_url"))
        api.update(job["id"], status="rendering", episode_id=d.name)
        return d, pipeline.run(cfg, d, voice=voice, log=log)
    if job["kind"] == "url":
        d = pipeline.new_episode_dir(cfg, p["url"].split("://", 1)[-1])
        api.update(job["id"], status="rendering", episode_id=d.name)
        return d, pipeline.run(cfg, d, target=p["url"], voice=voice, log=log)
    raise ValueError(f"unknown job kind {job['kind']}")
