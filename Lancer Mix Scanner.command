#!/bin/bash
# Double-cliquez sur ce fichier pour installer (la 1re fois) puis lancer Mix Scanner.
# Compatible avec les anciens Mac (Intel, macOS 10.13 et plus) : pas besoin de Homebrew.
cd "$(dirname "$0")" || exit 1
echo "🎧 Mix Scanner"
echo ""

pause_exit() { echo ""; read -r -p "Appuyez sur Entrée pour fermer…"; exit 1; }

# 1. Python 3.10 ou plus récent (installé depuis python.org)
PY=""
for p in python3.13 python3.12 python3.11 python3.10 python3; do
  for dir in /Library/Frameworks/Python.framework/Versions/Current/bin /usr/local/bin ""; do
    cand="${dir:+$dir/}$p"
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
      PY="$cand"; break 2
    fi
  done
done
if [ -z "$PY" ]; then
  echo "❌ Python 3.10 (ou plus récent) est nécessaire."
  echo "   1. Téléchargez « macOS 64-bit universal2 installer » sur la page qui va s'ouvrir"
  echo "      (prenez Python 3.12), puis installez-le."
  echo "   2. Double-cliquez à nouveau sur ce fichier."
  open "https://www.python.org/downloads/macos/"
  pause_exit
fi
echo "✔ Python : $("$PY" --version)"

# 2. ffmpeg / ffprobe : version autonome téléchargée dans le dossier de l'outil
mkdir -p bin
export PATH="$PWD/bin:$PATH"
for tool in ffmpeg ffprobe; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Téléchargement de $tool…"
    curl -fL --progress-bar -o "bin/$tool.zip" "https://evermeet.cx/ffmpeg/getrelease/$tool/zip" || pause_exit
    unzip -o -q "bin/$tool.zip" -d bin && rm "bin/$tool.zip"
    chmod +x "bin/$tool"
    xattr -d com.apple.quarantine "bin/$tool" 2>/dev/null
  fi
done
ffmpeg -version >/dev/null 2>&1 || { echo "❌ ffmpeg ne démarre pas sur ce Mac."; pause_exit; }
echo "✔ ffmpeg"

# 3. L'outil lui-même
if [ ! -x .venv/bin/python ]; then
  echo "Installation de l'outil (première fois uniquement, quelques minutes)…"
  "$PY" -m venv .venv || pause_exit
fi
source .venv/bin/activate
pip install -q --upgrade pip >/dev/null
pip install -q -r requirements.txt || pause_exit
pip install -q -U yt-dlp >/dev/null
echo "✔ Outil prêt"

(sleep 3; open "http://127.0.0.1:8000") &
echo ""
echo "✅ Mix Scanner tourne sur http://127.0.0.1:8000"
echo "   Pour l'arrêter : fermez cette fenêtre ou appuyez sur Ctrl + C."
echo ""
uvicorn mixscanner.web:app --host 127.0.0.1 --port 8000
