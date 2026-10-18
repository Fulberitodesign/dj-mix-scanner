"""Interface web : uvicorn mixscanner.web:app  puis ouvrir http://localhost:8000"""
from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from .export import EXPORTERS
from .pipeline import ScanOptions, ScanResult, scan
from .tracklist import build_tracklist

app = FastAPI(title="Mix Scanner")
STATIC = Path(__file__).parent / "static"


class ScanRequest(BaseModel):
    url: str
    step: float = 30
    clip: float = 12
    recognizer: str = "shazam"
    check_platforms: bool = True


class Job:
    def __init__(self, req: ScanRequest) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.req = req
        self.status = "queued"
        self.stage = "queued"
        self.progress = 0.0
        self.source: dict[str, Any] | None = None
        self.hits: list = []
        self.tracks: list = []
        self.warnings: list[str] = []
        self.error: str | None = None
        self.result: ScanResult | None = None
        self.lock = threading.Lock()

    def on_event(self, stage: str, data: dict[str, Any]) -> None:
        with self.lock:
            if stage == "warning":
                self.warnings = (self.warnings + [data["message"]])[-20:]
                return
            self.stage = stage
            if stage == "download":
                self.progress = data["progress"]
            elif stage == "source":
                self.source = data
            elif stage == "recognize":
                self.hits.append(data["hit"])
                self.progress = data["done"] / data["total"]
                # tracklist provisoire, affichée en direct
                self.tracks = build_tracklist(self.hits, self.req.clip, (self.source or {}).get("duration"))
            elif stage == "tracklist":
                self.tracks = data["tracks"]
                self.progress = 0.0
            elif stage == "platforms":
                self.progress = data["done"] / data["total"]

    def run(self) -> None:
        self.status = "running"
        opts = ScanOptions(step=self.req.step, clip=self.req.clip, recognizer=self.req.recognizer,
                           check_platforms=self.req.check_platforms)
        try:
            self.result = asyncio.run(scan(self.req.url, opts, self.on_event))
            self.tracks = self.result.tracks
            self.status = "done"
        except Exception as e:
            self.error = str(e)
            self.status = "error"

    def to_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.id, "status": self.status, "stage": self.stage, "progress": self.progress,
                "source": self.source, "error": self.error, "warnings": self.warnings,
                "windows_done": len(self.hits), "tracks": [t.to_dict() for t in self.tracks],
            }


JOBS: dict[str, Job] = {}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.post("/api/scan")
def start_scan(req: ScanRequest) -> dict[str, str]:
    if not req.url.strip():
        raise HTTPException(400, "Lien manquant")
    job = Job(req)
    JOBS[job.id] = job
    threading.Thread(target=job.run, daemon=True).start()
    return {"id": job.id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    if job_id not in JOBS:
        raise HTTPException(404, "Analyse introuvable")
    return JOBS[job_id].to_dict()


@app.get("/api/jobs/{job_id}/export.{fmt}")
def export(job_id: str, fmt: str) -> PlainTextResponse:
    job = JOBS.get(job_id)
    if not job or not job.result:
        raise HTTPException(404, "Analyse non terminée")
    if fmt not in EXPORTERS:
        raise HTTPException(400, "Format inconnu")
    media = {"csv": "text/csv", "json": "application/json", "md": "text/markdown"}.get(fmt, "text/plain")
    return PlainTextResponse(EXPORTERS[fmt](job.result), media_type=f"{media}; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="tracklist.{fmt}"'})
