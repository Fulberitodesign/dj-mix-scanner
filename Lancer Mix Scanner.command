#!/bin/bash
# Double-cliquez sur ce fichier pour installer (la 1re fois) puis lancer Mix Scanner.
cd "$(dirname "$0")" || exit 1
echo "🎧 Mix Scanner"

if ! command -v ffmpeg >/dev/null || ! command -v python3 >/dev/null; then
  if ! command -v brew >/dev/null; then
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
  fi
  if ! command -v brew >/dev/null; then
    echo "Installation de Homebrew (votre mot de passe Mac peut être demandé)…"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || exit 1
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
  fi
  echo "Installation de Python et ffmpeg…"
  brew install python ffmpeg || exit 1
fi

if [ ! -d .venv ]; then
  echo "Installation de l'outil (première fois uniquement)…"
  python3 -m venv .venv || exit 1
fi
source .venv/bin/activate
pip install -q --upgrade pip >/dev/null
pip install -q -r requirements.txt || exit 1
pip install -q -U yt-dlp >/dev/null

(sleep 3; open "http://127.0.0.1:8000") &
echo ""
echo "✅ Mix Scanner tourne sur http://127.0.0.1:8000"
echo "   Pour l'arrêter : fermez cette fenêtre ou appuyez sur Ctrl + C."
echo ""
uvicorn mixscanner.web:app --host 127.0.0.1 --port 8000
