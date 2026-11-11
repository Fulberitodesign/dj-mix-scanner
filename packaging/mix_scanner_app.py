"""Lanceur utilisé par PyInstaller pour construire « Mix Scanner.app »."""
import sys

from mixscanner.app import _setup_environment, main, run_window, selftest

if __name__ == "__main__":
    if "--selftest-window" in sys.argv:  # vérification de la vraie fenêtre (construction sur Mac)
        _setup_environment()
        raise SystemExit(run_window(selftest=True))
    raise SystemExit(selftest() if "--selftest" in sys.argv else main())
