# Audio Grabber

Downloads audio from YouTube, SoundCloud, and most other sites yt-dlp supports,
saved as mp3, m4a, opus, flac, wav, or aiff with metadata and cover art embedded.

Two front ends over the same engine:

- `gui.py` — a Tk window with a native folder picker and a progress bar
- `script.py` — an interactive command-line prompt loop

## Download the Mac app

Grab the `.dmg` from the [Releases page](../../releases/latest). Nothing else to
install — ffmpeg is bundled inside the app.

**Apple Silicon only** (M1 or newer). Check with  Apple menu → About This Mac.

The app isn't signed with a paid Apple developer certificate, so the first time
you open it macOS will refuse and say it's from an unidentified developer. That's
expected. Go to **System Settings → Privacy & Security**, scroll down, and click
**Open Anyway**.

## Running from source

Needs Python 3.10+ and ffmpeg.

```sh
brew install ffmpeg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python gui.py      # windowed
.venv/bin/python script.py   # command line
```

### Command-line options

```
--out FOLDER                 destination folder, skips the prompt
--format {mp3,m4a,opus,flac,wav,aiff}
--quality 192                kbps, ignored for the lossless formats
--cookies-from-browser NAME  safari, chrome, firefox, ... or none
```

The download folder, format, and cookie source are asked once at startup, stay
fixed for the session, and are remembered as next run's defaults in
`~/.config/ytgrab/config.json`.

## Cookies

Only needed for content behind a sign-in — private or age-restricted videos,
SoundCloud Go+ tracks, or when a site demands you prove you aren't a bot.
Ordinary public links need nothing, so leave it on `none` until a download is
actually refused.

Reading Safari's cookies requires granting your terminal (or the app) **Full Disk
Access** in System Settings → Privacy & Security, otherwise macOS blocks it.

## Playlists

A playlist or SoundCloud `/sets/` link downloads its first track only, and says
so. Point it at individual tracks if you want the whole set.

## Building the app

```sh
.venv/bin/pip install pyinstaller
.venv/bin/pyinstaller --noconfirm --windowed --name "Audio Grabber" \
  --add-binary "$(readlink -f $(which ffmpeg)):." \
  --add-binary "$(readlink -f $(which ffprobe)):." \
  gui.py
```
