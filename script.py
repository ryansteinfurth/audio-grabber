#!/usr/bin/env python3
"""
script.py - interactive personal audio downloader.

Asks for a destination folder once, then loops:
    URL  ->  confirm/edit filename (prefilled with the track title)  ->  download

Requires:  pip install -U yt-dlp
           ffmpeg on PATH  (brew install ffmpeg | apt install ffmpeg)

Usage:
    ./script.py                                 # mp3 @ 192k
    ./script.py --format opus                   # different codec
    ./script.py --out ~/Music/inbox             # skip the folder prompt
    ./script.py --cookies-from-browser safari   # skip the cookie prompt

The destination folder and cookie source are asked once at startup, stay fixed
for the whole session, and are remembered as next run's defaults.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

import yt_dlp
from yt_dlp.cookies import SUPPORTED_BROWSERS as _BROWSERS

BROWSERS = sorted(_BROWSERS)

# Remembered across runs: the last folder and browser you picked.
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "ytgrab" / "config.json"

def _use_bundled_ffmpeg() -> None:
    """A packaged .app ships its own ffmpeg; put it where everything can see it.

    PyInstaller unpacks bundled binaries into sys._MEIPASS. Prepending that to
    PATH means shutil.which() and yt-dlp both find it with no other changes."""
    base = getattr(sys, "_MEIPASS", None)
    if base and os.path.exists(os.path.join(base, "ffmpeg")):
        os.environ["PATH"] = base + os.pathsep + os.environ.get("PATH", "")


_use_bundled_ffmpeg()


# Illegal on Windows, awkward everywhere else.
ILLEGAL = r'[<>:"/\\|?*\x00-\x1f]'

CODECS = ["mp3", "m4a", "opus", "flac", "wav"]
ART_CAPABLE = {"mp3", "m4a", "flac"}


# ---------------------------------------------------------------- remembered settings

def load_config() -> dict:
    """Last session's answers. Any problem reading it just means no defaults."""
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def save_config(**values) -> None:
    """Best effort -- an unwritable config dir shouldn't sink the session."""
    cfg = load_config()
    cfg.update(values)
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- input helpers

def prefill_capable_readline():
    """The readline module if it can actually prefill a line, else None.

    macOS ships libedit, which exposes set_pre_input_hook but silently ignores
    it -- hasattr() alone can't tell the two backends apart."""
    try:
        import readline
    except ImportError:
        return None

    if not hasattr(readline, "set_pre_input_hook"):
        return None

    backend = getattr(readline, "backend", None)   # 3.13+ says so directly
    if backend is None:
        doc = readline.__doc__ or ""               # docstrings vanish under -OO
        backend = "editline" if "libedit" in doc else "readline"
    return readline if backend == "readline" else None


def input_with_default(prompt: str, default: str) -> str:
    """Show an editable, prefilled line. Falls back to [default] style where the
    terminal's readline can't prefill. Empty input means "take the default"."""
    readline = prefill_capable_readline()

    if readline is None:
        return input(f"{prompt}[{default}]: ").strip() or default

    def hook():
        readline.insert_text(default)
        readline.redisplay()

    readline.set_pre_input_hook(hook)
    try:
        return input(prompt).strip() or default
    finally:
        readline.set_pre_input_hook(None)


def sanitize(name: str) -> str:
    """Safe as a filename without mangling the title too much."""
    name = re.sub(ILLEGAL, "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(". ")          # Windows dislikes trailing dots/spaces
    return name[:180] or "untitled"


def clock(seconds) -> str:
    """m:ss (or h:mm:ss) from a duration that may be int, float, or None."""
    h, rem = divmod(int(seconds or 0), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def ask_folder(preset: str | None, remembered: str | None) -> Path:
    if preset:
        path = Path(preset).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    default = remembered or str(Path.home() / "Music" / "grabbed")
    while True:
        raw = input_with_default("Download folder: ", default)
        if not raw:
            continue
        path = Path(raw).expanduser().resolve()

        if path.exists() and not path.is_dir():
            print(f"  {path} exists and is not a folder.\n")
            continue
        if not path.exists():
            ans = input(f"  {path} doesn't exist. Create it? [Y/n] ").strip().lower()
            if ans and not ans.startswith("y"):
                continue
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"  Can't create it: {e}\n")
            continue
        if not os.access(path, os.W_OK):
            print("  That folder isn't writable.\n")
            continue
        return path


def resolve_collision(out_dir: Path, stem: str, ext: str) -> str | None:
    """Returns the stem to use, or None to skip this download."""
    target = out_dir / f"{stem}.{ext}"
    if not target.exists():
        return stem

    print(f"  {target.name} already exists.")
    try:
        choice = input("  [o]verwrite, [r]ename automatically, [s]kip? [r] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    choice = choice[:1] or "r"

    if choice == "o":
        try:
            target.unlink()
        except OSError as e:
            print(f"  Couldn't replace it: {e}")
            return None
        return stem
    if choice == "s":
        return None

    n = 2
    while (out_dir / f"{stem} ({n}).{ext}").exists():
        n += 1
    return f"{stem} ({n})"


def clean_partials(out_dir: Path, stem: str) -> None:
    """Drop the .part/.ytdl leftovers an interrupted download leaves behind.

    Matched by prefix rather than glob: titles routinely contain [brackets],
    which glob would read as character classes."""
    try:
        leftovers = list(out_dir.iterdir())
    except OSError:
        return
    for p in leftovers:
        if p.name.startswith(f"{stem}.") and p.suffix in (".part", ".ytdl"):
            try:
                p.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- yt-dlp glue

class QuietLogger:
    # yt-dlp calls these with extras like only_once=True, so swallow kwargs.
    def debug(self, msg, **kw):
        pass

    def info(self, msg, **kw):
        pass

    def warning(self, msg, **kw):
        pass

    def error(self, msg, **kw):
        print(msg, file=sys.stderr)


def cookie_opt(browser: str | None):
    """yt-dlp's cookiesfrombrowser tuple: (browser, profile, keyring, container)."""
    return (browser, None, None, None) if browser else None


# Sites we can name in the advisory below. yt-dlp handles ~1800 more, so a jar
# without these is worth mentioning but is never a reason to refuse.
COMMON_SITES = ("youtube", "soundcloud")


def cookie_problem(browser: str) -> tuple[str | None, str | None]:
    """(fatal, note). fatal means the cookies are unusable; note is advisory.

    The target site isn't known here -- cookies are chosen once at startup,
    before any URL is typed -- so a jar that simply lacks cookies for a site we
    recognise can't be treated as an error."""
    from yt_dlp.cookies import extract_cookies_from_browser

    try:
        jar = extract_cookies_from_browser(browser, logger=QuietLogger())
    except PermissionError:
        return (f"the OS blocked access to {browser}'s cookie store.\n"
                "  On macOS: System Settings > Privacy & Security > Full Disk Access,\n"
                "  add your terminal app, then restart it and try again."), None
    except FileNotFoundError:
        return f"no {browser} cookie store on this machine. Is {browser} installed?", None
    except Exception as e:
        return f"couldn't read {browser} cookies: {e}", None

    found = {s for s in COMMON_SITES if any(s in (c.domain or "") for c in jar)}
    if not found:
        return None, (f"{browser}'s cookies don't cover {' or '.join(COMMON_SITES)}. "
                      "They'll still be sent -- sign in there if a download is refused.")
    return None, None


def ask_browser(preset: str | None, remembered: str | None) -> str | None:
    """Pick a cookie source once. Returns a browser name, or None for no cookies."""
    options = "/".join(BROWSERS)

    if preset is not None:
        choice = preset.lower()
        if choice != "none" and choice not in BROWSERS:
            sys.exit(f"Unknown browser {choice!r}. Choose one of: {options}, none")
        if choice == "none":
            return None
        fatal, note = cookie_problem(choice)
        if fatal:                        # they asked for this explicitly, so don't paper over it
            sys.exit(f"Can't use {choice} cookies: {fatal}")
        if note:
            print(f"  Note: {note}")
        return choice

    while True:
        raw = input_with_default("Cookies from browser (safari/chrome/firefox/..., or none)? ",
                                 remembered or "none").lower()
        if raw != "none" and raw not in BROWSERS:
            print(f"  Not a browser yt-dlp supports. Pick from:\n    {options}, none\n")
            continue
        if raw == "none":
            return None
        fatal, note = cookie_problem(raw)
        if fatal:
            print(f"  Can't use {raw} cookies: {fatal}\n")
            again = input("  Try a different browser? [Y/n] ").strip().lower()
            if again and not again.startswith("y"):
                print("  Continuing without cookies.\n")
                return None
            continue
        if note:
            print(f"  Note: {note}")
        return raw


def probe(url: str, cookies=None) -> dict | None:
    """Fetch metadata without downloading, so we can offer the title."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "logger": QuietLogger(),
        "skip_download": True,
        "cookiesfrombrowser": cookies,
        # Only the first entry is ever used, so don't resolve the whole playlist.
        "playlist_items": "1",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except KeyboardInterrupt:
            print("\n  Cancelled.", file=sys.stderr)
            return None
        except Exception as e:                   # noqa: BLE001 - never crash the loop
            print(f"  Couldn't read that URL: {e}", file=sys.stderr)
            return None

    if info is None:
        return None

    # Playlists (YouTube playlists, SoundCloud /sets/, artist pages) come back
    # with an 'entries' key. playlist_items="1" caps that list at one, so its
    # length says nothing about the playlist -- but its presence still tells us
    # the URL was one, which is what the user needs to hear.
    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            print("  That playlist is empty.", file=sys.stderr)
            return None
        info = dict(entries[0], from_playlist=True)
    return info


def download(url: str, out_dir: Path, stem: str, codec: str, quality: str, cookies=None,
             progress=None, postprocess=None) -> bool:
    """progress/postprocess are yt-dlp hooks; the GUI uses them to drive its bar."""
    postprocessors = [
        {"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": quality},
        {"key": "FFmpegMetadata", "add_metadata": True},
    ]
    if codec in ART_CAPABLE:
        postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

    # outtmpl is %-templated, so a literal % in the user's name must be doubled.
    safe_stem = stem.replace("%", "%%")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / f"{safe_stem}.%(ext)s"),
        "postprocessors": postprocessors,
        "writethumbnail": codec in ART_CAPABLE,
        "logger": QuietLogger(),
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "noplaylist": True,
        "cookiesfrombrowser": cookies,
    }
    if progress:
        opts["progress_hooks"] = [progress]
    if postprocess:
        opts["postprocessor_hooks"] = [postprocess]

    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            return ydl.download([url]) == 0
        except KeyboardInterrupt:
            # Ctrl-C means "not this one", not "quit" -- the caller re-prompts.
            print("\n  Cancelled.", file=sys.stderr)
            clean_partials(out_dir, stem)
            return False
        except Exception as e:                   # noqa: BLE001 - never crash the loop
            print(f"  Download failed: {e}", file=sys.stderr)
            return False


# ---------------------------------------------------------------- main loop

def main():
    p = argparse.ArgumentParser(description="Interactive audio downloader.")
    p.add_argument("--out", help="destination folder (skips the folder prompt)")
    p.add_argument("--format", dest="codec", default="mp3", choices=CODECS)
    p.add_argument("--quality", default="192", help="kbps for lossy codecs (default 192)")
    p.add_argument("--cookies-from-browser", dest="browser", metavar="BROWSER",
                   help="pull cookies from a signed-in browser "
                        f"({'/'.join(BROWSERS)}, or none); skips the prompt")
    args = p.parse_args()

    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg isn't on your PATH, so every download would fail at the\n"
                 "audio-extraction step. Install it:  brew install ffmpeg")

    cfg = load_config()

    # Asked once, fixed for the whole session, remembered for the next one.
    try:
        out_dir = ask_folder(args.out, cfg.get("folder"))
        browser = ask_browser(args.browser, cfg.get("browser"))
    except (EOFError, KeyboardInterrupt):
        sys.exit("\n")

    cookies = cookie_opt(browser)
    save_config(folder=str(out_dir), browser=browser or "none")

    print(f"\nSaving {args.codec} files to {out_dir}")
    print(f"Cookies: {browser or 'not used'}")
    print("Paste a URL and press enter. Blank line or Ctrl-D to quit.\n")

    while True:
        try:
            url = input("URL: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not url or url.lower() in {"q", "quit", "exit"}:
            break
        if not url.startswith(("http://", "https://")):
            print("  That doesn't look like a URL.\n")
            continue

        print("  Reading...")
        info = probe(url, cookies)
        if info is None:
            print()
            continue

        # probe() resolves a playlist down to its first entry, so download that
        # exact track instead of handing yt-dlp the playlist URL again.
        track_url = info.get("webpage_url") or url

        if info.get("from_playlist"):
            print("  Note: that's a playlist. Using the first track only.")

        suggested = sanitize(info.get("title") or "untitled")
        uploader = info.get("uploader") or info.get("channel")
        duration = info.get("duration")
        if uploader:
            mins = f"  ({clock(duration)})" if duration else ""
            print(f"  {uploader}{mins}")

        try:
            stem = input_with_default("  Save as: ", suggested)
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.\n")
            continue

        stem = sanitize(stem) if stem else suggested
        stem = resolve_collision(out_dir, stem, args.codec)
        if stem is None:
            print("  Skipped.\n")
            continue

        ok = download(track_url, out_dir, stem, args.codec, args.quality, cookies)
        print(f"  {'Saved' if ok else 'Failed'}: {stem}.{args.codec}\n")

    print("Bye.")


if __name__ == "__main__":
    main()