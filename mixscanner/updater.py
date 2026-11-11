"""Mise à jour de l'application Mac depuis la Release GitHub « mac-app ».

1. check()   : compare le commit de l'app installée (inscrit dans _build.py à la construction) avec
               celui de la dernière Release ; renvoie la mise à jour disponible pour ce type de Mac.
2. install() : télécharge le DMG, l'ouvre, copie la nouvelle app à côté de l'actuelle, vérifie
               qu'elle démarre (autotest), referme le DMG et prépare un petit script qui, une fois
               l'app quittée, remplace l'ancienne version (avec retour arrière en cas d'échec) puis
               relance l'app. L'ancienne app n'est touchée qu'après toutes ces vérifications.

Le téléchargement ne passe pas par le navigateur : macOS ne redemande donc pas d'autorisation.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from . import __version__

REPO = "Fulberitodesign/dj-mix-scanner"
RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/tags/mac-app"
RELEASE_PAGE = f"https://github.com/{REPO}/releases/tag/mac-app"
APP_NAME = "Mix Scanner.app"


def build_info() -> dict:
    try:
        from . import _build
        return {"sha": _build.BUILD_SHA, "time": _build.BUILD_TIME, "arch": _build.ARCH}
    except Exception:
        return {"sha": None, "time": None, "arch": None}


def current_arch() -> str:
    info = build_info()
    if info["arch"]:
        return info["arch"]
    import platform
    return "AppleSilicon" if platform.machine() == "arm64" else "Intel"


def enabled() -> bool:
    """Seulement pour l'app Mac construite (pas en développement ni en ligne de commande)."""
    if os.environ.get("MIXSCANNER_UPDATES") == "0":
        return False
    return bool(build_info()["sha"]) and getattr(sys, "frozen", False) and sys.platform == "darwin"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Update:
    sha: str
    published: str
    url: str
    size: int
    name: str

    def to_dict(self) -> dict:
        return asdict(self)


def check(client: httpx.Client | None = None, info: dict | None = None) -> Update | None:
    """La mise à jour disponible pour ce Mac, ou None si l'app est à jour."""
    info = info or build_info()
    if not info["sha"]:
        return None
    own = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=True)
    try:
        r = client.get(RELEASE_API, headers={"Accept": "application/vnd.github+json",
                                             "User-Agent": f"MixScanner/{__version__}"})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        rel = r.json()
    finally:
        if own:
            client.close()
    sha = (rel.get("target_commitish") or "").strip()
    if not sha or sha.startswith(info["sha"]) or info["sha"].startswith(sha):
        return None
    published, built = _parse_time(rel.get("published_at")), _parse_time(info["time"])
    if published and built and published <= built:  # Release plus ancienne que l'app installée
        return None
    arch = info.get("arch") or current_arch()
    asset = next((a for a in rel.get("assets", []) if a.get("name", "").endswith(f"-{arch}.dmg")), None)
    if not asset:
        return None
    return Update(sha=sha, published=rel.get("published_at") or "", url=asset["browser_download_url"],
                  size=int(asset.get("size") or 0), name=asset["name"])


# --- Installation -------------------------------------------------------------------------

def current_bundle() -> Path | None:
    """Dossier « Mix Scanner.app » de l'app en cours (…/Mix Scanner.app/Contents/MacOS/Mix Scanner)."""
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None


def _run(cmd: list[str], timeout: float = 300) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} : {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def hdiutil_attach(dmg: Path) -> Path:
    mount = Path(tempfile.mkdtemp(prefix="mixscanner-maj-"))
    _run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount), str(dmg)])
    return mount


def hdiutil_detach(mount: Path) -> None:
    subprocess.run(["hdiutil", "detach", "-force", str(mount)], capture_output=True, timeout=120)


def download(update: Update, dest: Path, progress: Callable[[float], None] = lambda p: None,
             client: httpx.Client | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    own = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=True)
    try:
        with client.stream("GET", update.url, headers={"User-Agent": f"MixScanner/{__version__}"}) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or update.size or 0)
            done = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        progress(min(1.0, done / total))
    finally:
        if own:
            client.close()
    if update.size and tmp.stat().st_size != update.size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Téléchargement incomplet, réessayez.")
    tmp.replace(dest)
    return dest


SWAP_SCRIPT = """#!/bin/bash
# Remplace Mix Scanner par la nouvelle version une fois l'app quittée, puis la relance.
PID={pid}; TARGET={target}; STAGED={staged}; BACKUP={backup}; LOG={log}
exec >>"$LOG" 2>&1
echo "$(date) mise à jour : attente de la fin de l'app ($PID)"
for i in $(seq 1 120); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
rm -rf "$BACKUP"
if mv "$TARGET" "$BACKUP" && mv "$STAGED" "$TARGET"; then
  rm -rf "$BACKUP"
  xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null
  echo "$(date) mise à jour installée"
else
  echo "$(date) échec : retour à l'ancienne version"
  [ -d "$TARGET" ] || mv "$BACKUP" "$TARGET"
  rm -rf "$STAGED"
fi
{relaunch}
"""


def prepare(dmg: Path, bundle: Path, attach=hdiutil_attach, detach=hdiutil_detach,
            verify: bool = True) -> Path:
    """Copie la nouvelle app à côté de l'actuelle et vérifie qu'elle démarre. Renvoie son chemin."""
    if not os.access(bundle.parent, os.W_OK):
        raise RuntimeError(f"Impossible d'écrire dans « {bundle.parent} ». Glissez Mix Scanner dans votre "
                           "dossier Applications puis réessayez, ou mettez à jour à la main.")
    staged = bundle.parent / f".{bundle.stem}.maj.app"
    shutil.rmtree(staged, ignore_errors=True)
    mount = attach(dmg)
    try:
        new_app = mount / APP_NAME
        if not new_app.is_dir():
            raise RuntimeError("Le DMG téléchargé ne contient pas Mix Scanner.")
        ditto = shutil.which("ditto")
        if ditto:  # garde les liens, attributs et signature de l'app
            _run([ditto, str(new_app), str(staged)])
        else:
            shutil.copytree(new_app, staged, symlinks=True)
    finally:
        detach(mount)
    if verify:
        exe = staged / "Contents" / "MacOS" / bundle.stem
        out = subprocess.run([str(exe), "--selftest"], capture_output=True, text=True, timeout=300,
                             env={**os.environ, "MIXSCANNER_UPDATES": "0"})
        if out.returncode != 0 or "AUTOTEST RÉUSSI" not in out.stdout:
            shutil.rmtree(staged, ignore_errors=True)
            raise RuntimeError("La nouvelle version ne démarre pas correctement : mise à jour annulée.\n"
                               + out.stdout[-300:])
    return staged


def launch_swap(staged: Path, bundle: Path, relaunch: bool = True, pid: int | None = None) -> Path:
    """Écrit et lance (détaché) le script qui échangera les versions quand l'app sera quittée."""
    log_dir = Path.home() / "Library" / "Logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    script = Path(tempfile.mkdtemp(prefix="mixscanner-maj-")) / "installer.sh"
    script.write_text(SWAP_SCRIPT.format(
        pid=pid or os.getpid(), target=shlex.quote(str(bundle)), staged=shlex.quote(str(staged)),
        backup=shlex.quote(str(bundle.parent / f".{bundle.stem}.ancienne.app")),
        log=shlex.quote(str(log_dir / "MixScanner.log")),
        relaunch='open "$TARGET"' if relaunch else "true",
    ))
    script.chmod(0o755)
    subprocess.Popen(["/bin/bash", str(script)], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return script


# --- État partagé avec l'interface web ------------------------------------------------------

class Manager:
    """Vérifie au démarrage, puis installe à la demande. État lu par /api/update."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: dict = {"status": "disabled"}
        self.update: Update | None = None
        self.on_ready: Callable[[], None] = lambda: None

    def snapshot(self) -> dict:
        bundle = current_bundle() if enabled() else None
        with self.lock:
            return {**self.state, "current": {**build_info(), "version": __version__}, "page": RELEASE_PAGE,
                    # lancée depuis le DMG (lecture seule) : les mises à jour ne pourraient pas s'installer
                    "from_dmg": bool(bundle and str(bundle).startswith("/Volumes/"))}

    def _set(self, **kw) -> None:
        with self.lock:
            self.state = {**self.state, **kw}

    def start_check(self) -> None:
        if not enabled():
            return
        self._set(status="checking")

        def run() -> None:
            try:
                self.update = check()
                if self.update:
                    self._set(status="available", published=self.update.published, name=self.update.name,
                              size=self.update.size)
                else:
                    self._set(status="none")
            except Exception as e:  # pas de connexion : on réessaiera au prochain démarrage
                self._set(status="none", check_error=f"{type(e).__name__}: {e}"[:200])
        threading.Thread(target=run, name="maj-verif", daemon=True).start()

    def can_install(self) -> bool:
        with self.lock:
            return self.state.get("status") in ("available", "error") and self.update is not None

    def start_install(self) -> bool:
        with self.lock:
            if self.state.get("status") not in ("available", "error") or not self.update:
                return False
            self.state = {**self.state, "status": "downloading", "progress": 0.0, "error": None}

        def run() -> None:
            try:
                bundle = current_bundle()
                if not bundle:
                    raise RuntimeError("Emplacement de l'app introuvable.")
                cache = Path.home() / "Library" / "Caches" / "MixScanner"
                dmg = download(self.update, cache / self.update.name, lambda p: self._set(progress=p))
                self._set(status="installing")
                staged = prepare(dmg, bundle)
                dmg.unlink(missing_ok=True)
                launch_swap(staged, bundle)
                self._set(status="restarting")
                threading.Timer(1.5, self.on_ready).start()  # laisse la page afficher « Redémarrage… »
            except Exception as e:
                self._set(status="error", error=str(e)[:500])
        threading.Thread(target=run, name="maj-install", daemon=True).start()
        return True


MANAGER = Manager()


def selftest_update(dmg: Path) -> int:
    """Vérification réelle sur Mac (construction) : installe ce DMG par-dessus une copie de l'app."""
    bundle = current_bundle()
    if not bundle:
        print("ÉCHEC : lancez ce test depuis l'app construite")
        return 1
    work = Path(tempfile.mkdtemp(prefix="mixscanner-test-maj-"))
    target = work / APP_NAME
    _run(["ditto", str(bundle), str(target)])
    marker = target / "Contents" / "ancienne-version"
    marker.write_text("1")
    staged = prepare(dmg, target)
    print(f"OK   nouvelle version préparée et vérifiée : {staged.name}")
    launch_swap(staged, target, relaunch=False, pid=999999)  # processus inexistant : échange immédiat
    import time
    for _ in range(60):
        if not staged.exists() and target.exists() and not marker.exists():
            break
        time.sleep(0.5)
    swapped = target.exists() and not marker.exists() and not staged.exists()
    print(f"{'OK  ' if swapped else 'ÉCHEC'} échange des versions")
    out = subprocess.run([str(target / "Contents" / "MacOS" / bundle.stem), "--selftest"],
                         capture_output=True, text=True, timeout=300)
    started = "AUTOTEST RÉUSSI" in out.stdout
    print(f"{'OK  ' if started else 'ÉCHEC'} la version installée démarre")
    ok = swapped and started
    print("AUTOTEST MISE À JOUR", "RÉUSSI" if ok else "ÉCHOUÉ")
    shutil.rmtree(work, ignore_errors=True)
    return 0 if ok else 1
