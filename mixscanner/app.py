"""Point d'entrée de l'application Mac (« Mix Scanner.app ») : démarre l'interface web en arrière-plan
et l'ouvre dans le navigateur. Double-cliquer à nouveau sur l'app rouvre simplement la page.

L'app n'a pas d'icône dans le Dock : on l'arrête avec le bouton « Quitter » de la page, sinon elle
s'arrête seule après 2 heures sans utilisation.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8000  # adresse de retour prévue pour la connexion Spotify (http://127.0.0.1:8000/callback)
IDLE_LIMIT = 2 * 3600


def _bundle_dir() -> Path | None:
    return Path(getattr(sys, "_MEIPASS")) if getattr(sys, "frozen", False) else None


def _setup_environment() -> None:
    bundle = _bundle_dir()
    if bundle:
        # Deno (pour YouTube) et autres exécutables fournis avec l'app
        os.environ["PATH"] = f"{bundle / 'bin'}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"
    os.environ["MIXSCANNER_APP"] = "1"


def _setup_logging() -> Path:
    log_dir = Path.home() / "Library" / "Logs" if sys.platform == "darwin" else Path.home() / ".cache" / "mixscanner"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "MixScanner.log"
    logging.basicConfig(filename=log, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Sans terminal, les sorties standard n'existent pas : on les redirige vers le journal.
    stream = open(log, "a", buffering=1, encoding="utf-8")  # noqa: SIM115
    if sys.stdout is None or getattr(sys, "frozen", False):
        sys.stdout = stream
    if sys.stderr is None or getattr(sys, "frozen", False):
        sys.stderr = stream
    return log


def _already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/spotify/config", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, port)) != 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _open_when_ready(url: str, server) -> None:
    def run() -> None:
        for _ in range(300):
            if server.started:
                webbrowser.open(url)
                return
            time.sleep(0.1)
    threading.Thread(target=run, daemon=True).start()


def main() -> int:
    _setup_environment()
    log = _setup_logging()

    if _already_running(PORT):  # l'app tourne déjà : on rouvre juste la page
        webbrowser.open(f"http://{HOST}:{PORT}")
        return 0
    port = PORT if _port_free(PORT) else _free_port()
    if port != PORT:
        os.environ.setdefault("SPOTIFY_REDIRECT_URI", f"http://{HOST}:{port}/callback")

    import uvicorn

    from . import web

    config = uvicorn.Config(web.app, host=HOST, port=port, log_config=None, access_log=False)
    server = uvicorn.Server(config)
    web.on_quit(lambda: setattr(server, "should_exit", True))

    def watchdog() -> None:  # arrêt automatique après une longue inactivité
        while not server.should_exit:
            time.sleep(60)
            if web.idle_seconds() > IDLE_LIMIT and not web.busy():
                logging.info("Arrêt après %d s d'inactivité", IDLE_LIMIT)
                server.should_exit = True
    threading.Thread(target=watchdog, daemon=True).start()

    _open_when_ready(f"http://{HOST}:{port}", server)
    logging.info("Mix Scanner démarre sur le port %d (journal : %s)", port, log)
    server.run()
    return 0


def selftest() -> int:
    """Vérifie que l'app empaquetée contient tout ce qu'il faut (utilisé pendant la construction)."""
    import asyncio
    import subprocess
    import tempfile

    _setup_environment()
    import mixscanner  # noqa: F401  (configure les certificats)
    from shazamio import Shazam

    from .audio import extract_clip, ffmpeg_exe
    from .web import app  # noqa: F401  (FastAPI, uvicorn, page web)

    ok = True

    def check(name: str, fn) -> None:
        nonlocal ok
        try:
            print(f"OK   {name} : {fn()}")
        except Exception as e:
            ok = False
            print(f"ÉCHEC {name} : {type(e).__name__}: {e}")

    tone = Path(tempfile.mkdtemp()) / "tone.m4a"
    check("ffmpeg", lambda: subprocess.run(
        [ffmpeg_exe(), "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=20", str(tone)],
        check=True) and ffmpeg_exe())

    async def signature():
        wav = await extract_clip(tone, 5, 10)
        return (await Shazam().core_recognizer.recognize_bytes(value=wav, options=None)).signature.samples
    check("signature Shazam", lambda: asyncio.run(signature()))
    check("page web", lambda: (Path(__file__).parent / "static" / "index.html").stat().st_size)
    check("certificats", lambda: Path(os.environ["SSL_CERT_FILE"]).exists() or 1 / 0)

    def ytdlp():
        import yt_dlp
        from yt_dlp.utils import _jsruntime  # noqa: F401
        return yt_dlp.version.__version__
    check("yt-dlp", ytdlp)

    def deno():
        out = subprocess.run(["deno", "--version"], capture_output=True, text=True, check=True).stdout
        return out.splitlines()[0]
    check("deno (YouTube)", deno)
    print("AUTOTEST", "RÉUSSI" if ok else "ÉCHOUÉ")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(selftest() if "--selftest" in sys.argv else main())
