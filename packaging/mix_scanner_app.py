"""Lanceur utilisé par PyInstaller pour construire « Mix Scanner.app »."""
import sys

from mixscanner.app import _setup_environment, main, run_window, selftest

if __name__ == "__main__":
    if "--selftest-update" in sys.argv:  # installe le DMG donné par-dessus une copie de l'app (construction)
        from pathlib import Path

        from mixscanner.updater import selftest_update
        _setup_environment()
        raise SystemExit(selftest_update(Path(sys.argv[sys.argv.index("--selftest-update") + 1])))
    if "--selftest-window" in sys.argv:  # vérification de la vraie fenêtre (construction sur Mac)
        _setup_environment()
        raise SystemExit(run_window(selftest=True))
    raise SystemExit(selftest() if "--selftest" in sys.argv else main())
