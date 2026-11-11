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

## Application Mac (DMG)

La façon la plus simple : téléchargez le DMG sur la page
[Releases → « Mix Scanner pour Mac »](https://github.com/Fulberitodesign/dj-mix-scanner/releases/tag/mac-app)
(**AppleSilicon** pour les Mac M1/M2/M3/M4, **Intel** sinon), ouvrez-le et glissez « Mix Scanner » dans
Applications. Rien d'autre à installer (Python, ffmpeg et le module YouTube sont inclus), macOS 11 minimum.
La première ouverture demande une autorisation : voir `LISEZ-MOI.txt` dans le DMG.

L'app s'ouvre dans sa propre fenêtre (moteur WebKit de macOS) : les liens externes s'ouvrent dans le
navigateur, les exports passent par « Enregistrer sous », Cmd + Q quitte.
Les DMG sont reconstruits automatiquement par GitHub Actions (`.github/workflows/macos-app.yml`) ;
pour les construire soi-même sur un Mac : `packaging/macos/build.sh`.

## Installation

Prérequis : Python 3.10+ (ffmpeg est fourni automatiquement par `imageio-ffmpeg`). Sur Mac, vous pouvez
aussi double-cliquer sur « Lancer Mix Scanner.command », qui installe tout puis ouvre l'interface.

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
| `--cookies-from-browser chrome` | utiliser votre session YouTube (si YouTube demande « confirmez que vous n'êtes pas un robot ») |
| `-o fichier.ext` | export `.txt`, `.csv`, `.md`, `.json` ou `.playlist` (vérifié avant de lancer l'analyse) |

Un mix d'1 h ≈ 120 extraits. Les résultats sont mis en cache (`~/.cache/mixscanner`), y compris
pour les fichiers locaux : relancer le même mix ne refait pas la reconnaissance, et un scan interrompu
(Ctrl + C, fenêtre fermée) reprend là où il s'était arrêté.

### YouTube demande « confirmez que vous n'êtes pas un robot »
YouTube bloque parfois les téléchargements automatiques. Choisissez votre navigateur dans
**« Cookies YouTube »** (interface web) ou ajoutez `--cookies-from-browser chrome` (ligne de commande) :
l'outil utilise alors la session YouTube de ce navigateur (connectez-vous d'abord à YouTube dedans).
Safari demande d'autoriser le Terminal dans *Réglages Système → Confidentialité → Accès complet au disque*.
Pour que ce soit permanent : `export MIXSCANNER_COOKIES_FROM_BROWSER=chrome`.

## Créer une playlist Spotify

À la fin de l'analyse, la page propose un panneau **« Créer une playlist Spotify »** :

- **Méthode recommandée (gratuite, sans Premium)** : « Copier la liste » copie une ligne « Artiste - Titre »
  par morceau (sans doublons ni « ID »). Collez-la dans [TuneMyMusic](https://www.tunemymusic.com/fr/transfer)
  ou [Spotlistr](https://www.spotlistr.com/search/textbox), connectez votre compte Spotify : la playlist est créée.
- **Liens Spotify à coller dans l'app** : « Préparer les liens Spotify » cherche le lien de chaque morceau,
  puis vous les collez (Cmd + V) dans une playlist vide de l'app Spotify. Les liens viennent de la
  vérification Spotify (si vos clés sont configurées) ou de [MusicBrainz](https://musicbrainz.org) (gratuit,
  sans clé), qui connaît bien les morceaux grand public mais rarement les remix et edits de DJ.
  > L'API gratuite de song.link, utilisée auparavant, a été fermée en 2026. Si vous disposez d'une clé
  > song.link, définissez `ODESLI_API_KEY` pour l'utiliser en complément.
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
| `MIXSCANNER_COOKIES_FROM_BROWSER` | navigateur dont la session YouTube est utilisée (`chrome`, `safari`, `firefox`…) |
| `ODESLI_API_KEY` | clé song.link (facultative) pour trouver plus de liens Spotify |
| `MIXSCANNER_CACHE` | dossier du cache (défaut `~/.cache/mixscanner`) |

Deezer, Apple Music et YouTube sont vérifiés sans clé (Apple Music limite à ~20 recherches par minute :
l'outil espace ses requêtes et réutilise le lien Apple Music fourni par Shazam quand il existe).

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
