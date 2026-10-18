# 🎧 Mix Scanner

Collez le lien d'un mix DJ (YouTube, SoundCloud, Mixcloud… ou un fichier audio local) : l'outil
identifie **chaque morceau joué, dans l'ordre chronologique**, indique s'il s'agit d'un
**original, remix, mashup, edit, bootleg, VIP…**, puis vérifie s'il est **disponible sur Spotify,
SoundCloud, Deezer, Apple Music, YouTube** (+ liens de recherche Beatport et Google).

## Fonctionnement

1. **Téléchargement** de la piste audio avec `yt-dlp`.
2. **Découpage** : un extrait de 12 s toutes les 30 s (réglable) avec `ffmpeg`.
3. **Reconnaissance** de chaque extrait : Shazam (gratuit, sans clé), ACRCloud ou AudD.
4. **Tracklist** : les détections consécutives d'un même titre sont regroupées ; les transitions
   non reconnues ne coupent pas un morceau et les faux positifs isolés sont éliminés.
5. **Description YouTube** : si l'uploader a mis une tracklist horodatée (ou des chapitres), elle sert
   à confirmer les détections et à ajouter les morceaux que l'audio n'a pas reconnus.
6. **Plateformes** : chaque morceau est cherché (par ISRC quand c'est possible, sinon par titre/artiste)
   et l'outil indique si la version trouvée est la même (ex. le remix existe-t-il, ou seulement l'original ?).

## Installation

Prérequis : Python 3.10+ et [ffmpeg](https://ffmpeg.org/download.html).

```bash
pip install -r requirements.txt
```

## Utilisation

### Interface web
```bash
uvicorn mixscanner.web:app --port 8000
# puis ouvrir http://localhost:8000
```
La tracklist s'affiche en direct pendant l'analyse ; chaque horodatage renvoie au bon moment de la vidéo.
Export en Texte, CSV, Markdown ou JSON.

### Ligne de commande
```bash
python -m mixscanner "https://www.youtube.com/watch?v=XXXX" -o tracklist.csv
python -m mixscanner "https://www.youtube.com/watch?v=XXXX" --step 20 --recognizer acrcloud -o tracklist.md
python -m mixscanner mon_mix.mp3 --start 1800 --end 3600 --no-platforms
```

| Option | Rôle |
|---|---|
| `--step 30` | un extrait analysé toutes les N secondes (20 = plus précis, plus lent) |
| `--clip 12` | durée de chaque extrait |
| `--recognizer` | `shazam` (défaut), `acrcloud`, `audd` |
| `--min-hits 2` | ignore les morceaux détectés une seule fois (moins de faux positifs) |
| `--start / --end` | n'analyser qu'une partie du mix |
| `--no-platforms` | ne pas vérifier Spotify/SoundCloud/… |
| `--no-description` | ignorer la tracklist de la description |

Un mix d'1 h ≈ 120 extraits. Les résultats sont mis en cache (`~/.cache/mixscanner`) :
relancer la même vidéo ne refait pas la reconnaissance.

## Créer une playlist Spotify

À la fin de l'analyse, la page propose un panneau **« Créer une playlist Spotify »** :

- **Sans Spotify Premium** : « Copier la liste » copie une ligne « Artiste - Titre » par morceau
  (sans doublons ni « ID »). Collez-la dans [Spotlistr](https://www.spotlistr.com/search/textbox)
  ou [TuneMyMusic](https://www.tunemymusic.com/fr/transfer), connectez votre compte Spotify : la playlist est créée.
- **Connexion directe** (si `SPOTIFY_CLIENT_ID` est défini et que l'app Spotify a la « Web API », réservée
  aux comptes Premium) : l'outil crée lui-même la playlist privée sur votre compte. Ajoutez
  `http://127.0.0.1:8000/callback` dans les « Redirect URIs » de l'app.

## Clés optionnelles (variables d'environnement)

| Variable | Utilité |
|---|---|
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | vérification Spotify ([clé gratuite](https://developer.spotify.com/dashboard)) |
| `SOUNDCLOUD_CLIENT_ID` | vérification SoundCloud (sinon : lien de recherche) |
| `ACRCLOUD_HOST`, `ACRCLOUD_ACCESS_KEY`, `ACRCLOUD_ACCESS_SECRET` | reconnaissance ACRCloud — **la plus fiable sur les mixes DJ** (pitch/tempo modifiés, voix MC) |
| `AUDD_API_TOKEN` | reconnaissance AudD |

Deezer, Apple Music et YouTube sont vérifiés sans clé.

Statuts : ✅ disponible · « autre version » = le titre existe mais pas cette version (ex. remix absent,
seul l'original est sorti) · ❌ introuvable · 🔎 lien de recherche non vérifié.

## Limites

- Les morceaux inédits (« ID - ID »), bootlegs non publiés et passages très modifiés (pitch, effets,
  superpositions longues) peuvent ne pas être reconnus. ACRCloud et un `--step` plus petit aident.
- Shazam est utilisé via une API non officielle (`shazamio`) qui peut limiter le débit.
- Téléchargez uniquement des contenus que vous avez le droit d'analyser (conditions d'utilisation de YouTube).

## Tests
```bash
python -m pytest
```
