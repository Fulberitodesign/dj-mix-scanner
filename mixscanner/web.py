"""Interface web : uvicorn mixscanner.web:app  puis ouvrir http://localhost:8000"""
from __future__ import annotations

import asyncio
import html
import secrets
import threading
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from . import odesli, spotify_playlist
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
        self.warning_count = 0
        self.error: str | None = None
        self.result: ScanResult | None = None
        self.spotify_links: dict[str, Any] = {"status": "idle"}
        self.lock = threading.Lock()

    def on_event(self, stage: str, data: dict[str, Any]) -> None:
        with self.lock:
            if stage == "warning":
                self.warnings = (self.warnings + [data["message"]])[-20:]
                self.warning_count += 1
                return
            if stage == "retry":
                self.stage = "retry"
                return
            self.stage = stage
            if stage == "download":
                self.progress = data["progress"]
            elif stage == "source":
                self.source = data
            elif stage == "recognize":
                self.hits = [h for h in self.hits if h.offset != data["hit"].offset] + [data["hit"]]
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

    def resolve_spotify_links(self) -> None:
        """Cherche le lien Spotify de chaque morceau (via song.link), en arrière-plan."""
        def progress(done: int, total: int) -> None:
            self.spotify_links.update(done=done, total=total)

        async def run() -> tuple[list[str], list]:
            async with httpx.AsyncClient(timeout=20) as client:
                return await odesli.resolve_spotify_links(self.result.tracks, client, progress)

        self.spotify_links = {"status": "running", "done": 0, "total": 0}
        try:
            links, missing = asyncio.run(run())
            self.spotify_links = {"status": "done", "links": links,
                                  "missing": [f"{t.artist} - {t.title}" for t in missing]}
        except Exception as e:
            self.spotify_links = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    def to_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "spotify_links": self.spotify_links,
                "id": self.id, "status": self.status, "stage": self.stage, "progress": self.progress,
                "source": self.source, "error": self.error, "warnings": self.warnings, "warning_count": self.warning_count,
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


@app.post("/api/jobs/{job_id}/spotify-links")
def start_spotify_links(job_id: str) -> dict[str, str]:
    job = JOBS.get(job_id)
    if not job or not job.result:
        raise HTTPException(404, "Analyse non terminée")
    if job.spotify_links.get("status") != "running":
        threading.Thread(target=job.resolve_spotify_links, daemon=True).start()
    return {"status": "running"}


@app.get("/api/jobs/{job_id}/export.{fmt}")
def export(job_id: str, fmt: str) -> PlainTextResponse:
    job = JOBS.get(job_id)
    if not job or not job.result:
        raise HTTPException(404, "Analyse non terminée")
    if fmt not in EXPORTERS:
        raise HTTPException(400, "Format inconnu")
    media = {"csv": "text/csv", "json": "application/json", "md": "text/markdown"}.get(fmt, "text/plain")
    filename = "playlist.txt" if fmt == "playlist" else f"tracklist.{fmt}"
    return PlainTextResponse(EXPORTERS[fmt](job.result), media_type=f"{media}; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# --- Playlist Spotify (connexion directe, nécessite une app avec la Web API) -----------

_PENDING: dict[str, tuple[str, str]] = {}  # state -> (code_verifier, job_id)


@app.get("/api/spotify/config")
def spotify_config() -> dict[str, Any]:
    return {"oauth": bool(spotify_playlist.client_id()), "redirect_uri": spotify_playlist.redirect_uri()}


@app.get("/spotify/login")
def spotify_login(job: str) -> RedirectResponse:
    cid = spotify_playlist.client_id()
    if not cid:
        raise HTTPException(400, "SPOTIFY_CLIENT_ID n'est pas configuré")
    if job not in JOBS or not JOBS[job].result:
        raise HTTPException(404, "Analyse non terminée")
    verifier, challenge = spotify_playlist.pkce_pair()
    state = secrets.token_urlsafe(16)
    _PENDING[state] = (verifier, job)
    return RedirectResponse(spotify_playlist.authorize_url(cid, challenge, state))


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;
background:#111114;color:#ececf1}}a{{color:#9d86ff}}li{{color:#9a9aa6}}</style></head>
<body><h1>{html.escape(title)}</h1>{body}<p><a href="/">← Retour à Mix Scanner</a></p></body></html>""")


@app.get("/callback")
async def spotify_callback(state: str = "", code: str = "", error: str = "") -> HTMLResponse:
    pending = _PENDING.pop(state, None)
    if error or not pending:
        return _page("Connexion Spotify annulée", f"<p>{html.escape(error or 'Session expirée, réessayez.')}</p>")
    verifier, job_id = pending
    job = JOBS.get(job_id)
    if not job or not job.result:
        return _page("Analyse introuvable", "<p>Relancez l'analyse puis réessayez.</p>")
    res = job.result
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await spotify_playlist.exchange_code(client, spotify_playlist.client_id(), code, verifier)
            pl = await spotify_playlist.create_playlist(
                client, token, res.source["title"], f"Tracklist détectée par Mix Scanner — {res.source.get('url', '')}",
                res.tracks)
    except Exception as e:
        return _page("Impossible de créer la playlist", f"<p>{html.escape(str(e))}</p>")
    missing = "".join(f"<li>{html.escape(t.artist)} - {html.escape(t.title)}</li>" for t in pl.missing)
    body = (f'<p>✅ {len(pl.added)} morceaux ajoutés. <a href="{html.escape(pl.url)}" target="_blank">'
            f"Ouvrir la playlist dans Spotify</a></p>")
    if missing:
        body += f"<p>Introuvables sur Spotify ({len(pl.missing)}) :</p><ul>{missing}</ul>"
    return _page("Playlist Spotify créée", body)
