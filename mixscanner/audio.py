"""Téléchargement de l'audio (YouTube, SoundCloud, Mixcloud... via yt-dlp) et extraction d'extraits."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class MixSource:
    path: Path
    title: str
    duration: float
    url: str | None = None
    uploader: str | None = None
    description: str = ""
    chapters: list[dict[str, Any]] = field(default_factory=list)
    video_id: str | None = None


def _require_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("ffmpeg/ffprobe est requis (https://ffmpeg.org/download.html).")


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def load_source(
    url_or_path: str,
    cache_dir: Path,
    on_progress: Callable[[float], None] | None = None,
) -> MixSource:
    """Télécharge la piste audio d'un lien (ou ouvre un fichier local)."""
    _require_ffmpeg()
    local = Path(url_or_path).expanduser()
    if local.exists():
        return MixSource(path=local, title=local.stem, duration=probe_duration(local))

    import yt_dlp

    cache_dir.mkdir(parents=True, exist_ok=True)

    def hook(d: dict[str, Any]) -> None:
        if on_progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                on_progress(d.get("downloaded_bytes", 0) / total)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(cache_dir / "%(extractor)s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url_or_path, download=True)
        path = Path(ydl.prepare_filename(info))

    return MixSource(
        path=path,
        title=info.get("title") or path.stem,
        duration=float(info.get("duration") or probe_duration(path)),
        url=info.get("webpage_url") or url_or_path,
        uploader=info.get("uploader"),
        description=info.get("description") or "",
        chapters=info.get("chapters") or [],
        video_id=info.get("id"),
    )


async def extract_clip(path: Path, offset: float, length: float) -> bytes:
    """Extrait `length` secondes à partir de `offset`, en WAV mono 16 kHz (en mémoire)."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-v", "error",
        "-ss", f"{offset:.2f}", "-i", str(path), "-t", f"{length:.2f}",
        "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg a échoué à {offset:.0f}s : {err.decode(errors='ignore')[:300]}")
    return out


def default_cache_dir() -> Path:
    base = os.environ.get("MIXSCANNER_CACHE") or Path.home() / ".cache" / "mixscanner"
    return Path(base)
