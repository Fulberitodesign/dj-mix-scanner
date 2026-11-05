"""Lanceur utilisé par PyInstaller pour construire « Mix Scanner.app »."""
import sys

from mixscanner.app import main, selftest

if __name__ == "__main__":
    raise SystemExit(selftest() if "--selftest" in sys.argv else main())
