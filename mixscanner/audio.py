"""Téléchargement de l'audio (YouTube, SoundCloud, Mixcloud... via yt-dlp) et extraction d'extraits."""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import shutil
import subprocess
import wave
from functools import cache
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
    cache_id: str | None = None  # identifiant stable pour le cache des reconnaissances


BROWSERS = ("chrome", "safari", "firefox", "edge", "brave", "chromium", "opera", "vivaldi")


class DownloadError(RuntimeError):
    """Téléchargement impossible, avec un message compréhensible."""


def explain_download_error(message: str) -> str:
    low = message.lower()
    if "confirm you" in low and "bot" in low or "sign in to confirm" in low:
        return ("YouTube demande une vérification « vous n'êtes pas un robot ». Choisissez votre navigateur "
                "dans « Cookies YouTube » (ou --cookies-from-browser chrome en ligne de commande) : l'outil "
                "utilisera votre session YouTube. Sinon, réessayez plus tard ou depuis une autre connexion.")
    if "cookies database" in low and ("could not find" in low or "not found" in low):
        return ("Ce navigateur n'est pas installé (ou n'a jamais été ouvert) : dans « Cookies YouTube », choisissez "
                "le navigateur que vous utilisez et dans lequel vous êtes connecté à YouTube.")
    if "could not copy" in low and "cookie" in low or "cookies" in low and ("permission" in low or "keyring" in low):
        return ("Impossible de lire les cookies du navigateur. Fermez le navigateur et réessayez ; pour Safari, "
                "autorisez le Terminal dans Réglages Système → Confidentialité → Accès complet au disque.")
    if "[audiomack]" in low:
        return ("Audiomack n'est pas pris en charge pour l'instant : le site a fermé l'accès utilisé pour "
                "télécharger les mix. Si l'artiste autorise le téléchargement sur Audiomack, téléchargez le mix "
                "puis analysez le fichier audio ; sinon cherchez le même mix sur YouTube ou SoundCloud.")
    if "[mixcloud]" in low and ("403" in low or "forbidden" in low):
        return ("Mixcloud a refusé le téléchargement (protection anti-robots). Réessayez plus tard, ou cherchez "
                "le même mix sur YouTube ou SoundCloud.")
    if "private video" in low or "vidéo privée" in low:
        return "Cette vidéo est privée."
    if "unsupported url" in low:
        return "Lien non pris en charge. Collez le lien d'une vidéo YouTube, d'un son SoundCloud ou d'un mix Mixcloud."
    if "age" in low and ("restricted" in low or "confirm your age" in low):
        return "Vidéo soumise à une limite d'âge : choisissez votre navigateur dans « Cookies YouTube »."
    if "unable to download" in low or "http error" in low or "timed out" in low:
        return f"Le téléchargement a échoué (connexion ou site indisponible). Détail : {message[:300]}"
    return f"Téléchargement impossible : {message[:400]}"


@cache
def ffmpeg_exe() -> str:
    """ffmpeg du système, sinon celui fourni par le paquet imageio-ffmpeg (aucune installation à faire).

    Dans ce second cas, un lien nommé « ffmpeg » est ajouté au PATH : le module Shazam
    (shazamio_core) appelle « ffmpeg » directement et ne trouverait pas le binaire sinon.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise RuntimeError("ffmpeg est introuvable : pip install imageio-ffmpeg") from None
    bin_dir = default_cache_dir() / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / "ffmpeg"
    if not link.exists() or link.resolve() != Path(exe).resolve():
        link.unlink(missing_ok=True)
        link.symlink_to(exe)
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    return str(link)


def probe_duration(path: Path) -> float:
    out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out.stderr)
    if not m:
        raise RuntimeError(f"Impossible de lire la durée de {path}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _local_cache_id(path: Path) -> str:
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    return "local-" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def load_source(
    url_or_path: str,
    cache_dir: Path,
    on_progress: Callable[[float], None] | None = None,
    cookies_from_browser: str | None = None,
) -> MixSource:
    """Télécharge la piste audio d'un lien (ou ouvre un fichier local)."""
    url_or_path = url_or_path.strip()
    local = Path(url_or_path).expanduser()
    if "://" not in url_or_path and local.is_file():
        return MixSource(path=local, title=local.stem, duration=probe_duration(local),
                         cache_id=_local_cache_id(local))

    import yt_dlp

    cache_dir.mkdir(parents=True, exist_ok=True)

    def hook(d: dict[str, Any]) -> None:
        if not on_progress or d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if total:
            on_progress(min(1.0, d.get("downloaded_bytes", 0) / total))
        elif d.get("fragment_count"):  # flux HLS (SoundCloud, Mixcloud…) : progression par fragment
            on_progress(min(1.0, (d.get("fragment_index") or 0) / d["fragment_count"]))

    opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": str(cache_dir / "%(extractor)s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "ffmpeg_location": ffmpeg_exe(),
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 10,
    }
    browser = (cookies_from_browser or os.environ.get("MIXSCANNER_COOKIES_FROM_BROWSER") or "").strip().lower()
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url_or_path, download=True)
            if info.get("entries"):  # lien de playlist / set : on prend le premier élément
                info = next(e for e in info["entries"] if e)
            downloads = info.get("requested_downloads") or []
            path = Path(downloads[0]["filepath"]) if downloads and downloads[0].get("filepath") \
                else Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as e:
        raise DownloadError(explain_download_error(str(e))) from None
    except StopIteration:
        raise DownloadError("Cette playlist est vide.") from None
    if not path.exists():
        raise DownloadError(f"Le fichier téléchargé est introuvable ({path.name}).")

    video_id = info.get("id")
    return MixSource(
        path=path,
        title=info.get("title") or path.stem,
        duration=float(info.get("duration") or probe_duration(path)),
        url=info.get("webpage_url") or url_or_path,
        uploader=info.get("uploader"),
        description=info.get("description") or "",
        chapters=info.get("chapters") or [],
        video_id=video_id,
        cache_id=f"{info.get('extractor_key') or info.get('extractor') or 'web'}-{video_id}".lower() if video_id else None,
    )


SAMPLE_RATE = 16000


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    """Encapsule du PCM 16 bits mono dans un WAV avec des tailles exactes."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


async def extract_clip(path: Path, offset: float, length: float) -> bytes:
    """Extrait `length` secondes à partir de `offset`, en WAV mono 16 kHz (en mémoire).

    ffmpeg ne peut pas écrire la taille réelle dans l'en-tête d'un WAV envoyé dans un pipe
    (il met 0xFFFFFFFF) et shazamio_core 2.x refuse ce fichier (« unexpected end of file ») :
    on récupère donc du PCM brut et on construit l'en-tête nous-mêmes.
    """
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_exe(), "-nostdin", "-v", "error",
        "-ss", f"{offset:.2f}", "-i", str(path), "-t", f"{length:.2f}",
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg a échoué à {offset:.0f}s : {err.decode(errors='ignore')[:300]}")
    return pcm_to_wav(out)


def default_cache_dir() -> Path:
    base = os.environ.get("MIXSCANNER_CACHE") or Path.home() / ".cache" / "mixscanner"
    return Path(base)
