"""Point d'entrée de l'application Mac (« Mix Scanner.app »).

L'interface s'affiche dans sa propre fenêtre (moteur WebKit de macOS, via pywebview) : pas de
navigateur. Le serveur local qui fait les analyses tourne en arrière-plan dans l'app et s'arrête avec
elle (Cmd + Q ou fermeture de la fenêtre). Les liens externes (Spotify, Deezer, TuneMyMusic…)
s'ouvrent dans le navigateur ; les exports passent par la boîte « Enregistrer sous » de macOS.

Si la fenêtre ne peut pas être créée, l'app se rabat sur le navigateur (option --browser).
"""
from __future__ import annotations

import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8000  # adresse de retour prévue pour la connexion Spotify (http://127.0.0.1:8000/callback)
IDLE_LIMIT = 2 * 3600  # mode navigateur seulement : arrêt automatique après 2 h sans utilisation

FR = {
    "global.quitConfirmation": "Voulez-vous vraiment quitter ?",
    "global.ok": "OK",
    "global.quit": "Quitter",
    "global.cancel": "Annuler",
    "global.saveFile": "Enregistrer le fichier",
    "cocoa.menu.about": "À propos de",
    "cocoa.menu.services": "Services",
    "cocoa.menu.view": "Présentation",
    "cocoa.menu.edit": "Édition",
    "cocoa.menu.hide": "Masquer",
    "cocoa.menu.hideOthers": "Masquer les autres",
    "cocoa.menu.showAll": "Tout afficher",
    "cocoa.menu.quit": "Quitter",
    "cocoa.menu.fullscreen": "Plein écran",
    "cocoa.menu.cut": "Couper",
    "cocoa.menu.copy": "Copier",
    "cocoa.menu.paste": "Coller",
    "cocoa.menu.selectAll": "Tout sélectionner",
}


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
    if sys.stdout is None or sys.stderr is None or getattr(sys, "frozen", False):
        stream = open(log, "a", buffering=1, encoding="utf-8")  # noqa: SIM115
        sys.stdout = sys.stdout if sys.stdout is not None and not getattr(sys, "frozen", False) else stream
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


def _choose_port() -> int:
    port = PORT if _port_free(PORT) else _free_port()
    if port != PORT:
        os.environ.setdefault("SPOTIFY_REDIRECT_URI", f"http://{HOST}:{port}/callback")
    return port


def _make_server(port: int):
    import uvicorn

    from . import web

    config = uvicorn.Config(web.app, host=HOST, port=port, log_config=None, access_log=False)
    return uvicorn.Server(config)


def _start_server_thread(port: int):
    """Démarre le serveur local dans un fil d'exécution ; renvoie (serveur, fil) une fois prêt."""
    server = _make_server(port)
    thread = threading.Thread(target=server.run, name="serveur", daemon=True)
    thread.start()
    for _ in range(300):
        if server.started:
            return server, thread
        if not thread.is_alive():
            break
        time.sleep(0.1)
    raise RuntimeError(f"Le serveur local n'a pas démarré sur le port {port}")


# --- Fenêtre native ---------------------------------------------------------------------

class DesktopApi:
    """Fonctions appelables depuis la page (window.pywebview.api.…) dans la fenêtre de l'app.
    Les attributs commençant par « _ » ne sont pas exposés à la page."""

    def __init__(self, base_url: str) -> None:
        self._base = base_url
        self._window = None

    def copy(self, text: str) -> bool:
        """Copie dans le presse-papiers de macOS (plus fiable que celui de la page)."""
        try:
            from AppKit import NSPasteboard, NSPasteboardTypeString

            pb = NSPasteboard.generalPasteboard()
            pb.clearContents()
            return bool(pb.setString_forType_(text, NSPasteboardTypeString))
        except Exception:
            logging.exception("Copie dans le presse-papiers impossible")
            return False

    def save_export(self, job_id: str, fmt: str) -> str | None:
        """Boîte « Enregistrer sous » puis écriture de l'export ; renvoie le chemin choisi."""
        import webview

        if not re.fullmatch(r"[0-9a-f]{6,32}", job_id or "") or not re.fullmatch(r"[a-z]{2,10}", fmt or ""):
            return None
        with urllib.request.urlopen(f"{self._base}/api/jobs/{job_id}/export.{fmt}", timeout=30) as r:
            data = r.read()
            m = re.search(r'filename="([^"]+)"', r.headers.get("Content-Disposition", ""))
        filename = m.group(1) if m else f"tracklist.{fmt}"
        downloads = Path.home() / "Downloads"
        chosen = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=str(downloads),
                                                 save_filename=filename)
        if not chosen:
            return None
        path = Path(chosen if isinstance(chosen, str) else chosen[0])
        path.write_bytes(data)
        return str(path)

    def open_url(self, url: str) -> bool:
        if not re.match(r"https?://", url or ""):
            return False
        return webbrowser.open(url)


def _create_window(url: str, api: DesktopApi):
    import webview

    from . import web

    window = webview.create_window(
        "Mix Scanner", url, js_api=api, width=1200, height=880, min_size=(720, 520),
        text_select=True, background_color="#F6F6F8",
    )
    api._window = window
    web.on_quit(window.destroy)
    from .updater import MANAGER
    MANAGER.on_ready = window.destroy  # la mise à jour est prête : on quitte, le script relance l'app
    MANAGER.start_check()
    return window


def run_window(selftest: bool = False) -> int:
    import webview

    os.environ["MIXSCANNER_WINDOW"] = "1"
    server = thread = None
    if _already_running(PORT):  # une autre instance (ou la ligne de commande) sert déjà l'interface
        port = PORT
    else:
        port = _choose_port()
        server, thread = _start_server_thread(port)
    url = f"http://{HOST}:{port}"
    api = DesktopApi(url)
    window = _create_window(url, api)
    result = {"ok": not selftest}

    if selftest:
        threading.Thread(target=_window_selftest, args=(window, api, result), daemon=True).start()
        threading.Thread(target=lambda: (time.sleep(150), print("AUTOTEST FENÊTRE : délai dépassé"),
                                         os._exit(3)), daemon=True).start()

    logging.info("Mix Scanner (fenêtre) sur %s", url)
    webview.start(localization=FR, private_mode=False)
    if server:
        server.should_exit = True
        thread.join(timeout=10)
    return 0 if result["ok"] else 1


def _window_selftest(window, api: DesktopApi, result: dict) -> None:
    """Vérifie la fenêtre réelle : page chargée, pont Python ↔ page, presse-papiers, puis ferme."""
    ok = True

    def check(name: str, value, expected) -> None:
        nonlocal ok
        good = value == expected
        ok = ok and good
        print(f"{'OK  ' if good else 'ÉCHEC'} {name} : {value!r}", flush=True)

    try:
        check("page chargée", window.events.loaded.wait(60), True)
        bridge = None
        for _ in range(60):
            bridge = window.evaluate_js("window.pywebview && window.pywebview.api && typeof window.pywebview.api.copy")
            if bridge == "function":
                break
            time.sleep(0.5)
        check("pont page ↔ app", bridge, "function")
        check("titre", window.evaluate_js("document.title"), "Mix Scanner")
        time.sleep(2)  # laisse la page interroger /api/app
        check("bouton Quitter masqué (fenêtre)", window.evaluate_js("document.getElementById('quit').hidden"), True)
        check("formulaire présent", window.evaluate_js("!!document.getElementById('url')"), True)
        check("copie presse-papiers", api.copy("mixscanner-autotest"), True)
        try:
            from AppKit import NSPasteboard, NSPasteboardTypeString
            pasted = NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
        except Exception as e:
            pasted = f"{type(e).__name__}: {e}"
        check("presse-papiers relu", pasted, "mixscanner-autotest")
    except Exception as e:
        ok = False
        print(f"ÉCHEC autotest fenêtre : {type(e).__name__}: {e}", flush=True)
    print("AUTOTEST FENÊTRE", "RÉUSSI" if ok else "ÉCHOUÉ", flush=True)
    result["ok"] = ok
    window.destroy()


# --- Secours : interface dans le navigateur -----------------------------------------------

def run_browser() -> int:
    from . import web

    if _already_running(PORT):  # l'app tourne déjà : on rouvre juste la page
        webbrowser.open(f"http://{HOST}:{PORT}")
        return 0
    port = _choose_port()
    server = _make_server(port)
    web.on_quit(lambda: setattr(server, "should_exit", True))
    from .updater import MANAGER
    MANAGER.on_ready = lambda: setattr(server, "should_exit", True)
    MANAGER.start_check()

    def watchdog() -> None:  # arrêt automatique après une longue inactivité
        while not server.should_exit:
            time.sleep(60)
            if web.idle_seconds() > IDLE_LIMIT and not web.busy():
                logging.info("Arrêt après %d s d'inactivité", IDLE_LIMIT)
                server.should_exit = True
    threading.Thread(target=watchdog, daemon=True).start()

    def open_when_ready() -> None:
        for _ in range(300):
            if server.started:
                webbrowser.open(f"http://{HOST}:{port}")
                return
            time.sleep(0.1)
    threading.Thread(target=open_when_ready, daemon=True).start()
    logging.info("Mix Scanner (navigateur) sur le port %d", port)
    server.run()
    return 0


def main() -> int:
    _setup_environment()
    log = _setup_logging()
    logging.info("Démarrage (journal : %s)", log)
    if "--browser" not in sys.argv:
        try:
            import webview  # noqa: F401
        except Exception:
            logging.exception("Fenêtre indisponible : ouverture dans le navigateur")
        else:
            try:
                return run_window()
            except Exception:
                logging.exception("La fenêtre n'a pas pu s'ouvrir : ouverture dans le navigateur")
    return run_browser()


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
            print(f"OK   {name} : {fn()}", flush=True)
        except Exception as e:
            ok = False
            print(f"ÉCHEC {name} : {type(e).__name__}: {e}", flush=True)

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

    if sys.platform == "darwin":
        def window_lib():
            import webview
            from webview.platforms import cocoa  # noqa: F401  (WebKit, AppKit)
            return webview.__version__ if hasattr(webview, "__version__") else "ok"
        check("fenêtre (pywebview + WebKit)", window_lib)

    print("AUTOTEST", "RÉUSSI" if ok else "ÉCHOUÉ", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest-window" in sys.argv:
        _setup_environment()
        raise SystemExit(run_window(selftest=True))
    raise SystemExit(selftest() if "--selftest" in sys.argv else main())
