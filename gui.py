#!/usr/bin/env python3
"""
gui.py - windowed front end for the audio downloader.

Same engine as script.py (probe / download / cookies / remembered settings),
driven from a Tk window instead of prompts, with the native folder picker for
choosing the destination.

Run:  ./gui.py          (needs the same yt-dlp + ffmpeg as the CLI)
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import script  # probe(), download(), cookie helpers, config, CODECS, BROWSERS

# Bitrate is meaningless for these, so grey the field out.
LOSSLESS = {"wav", "flac", "aiff"}

NO_COOKIES = "none"


def human(n: float | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue[tuple] = queue.Queue()
        self.info: dict | None = None      # last successful probe
        self.probed_url: str | None = None
        self.busy = False

        cfg = script.load_config()
        root.title("Audio Grabber")
        root.minsize(620, 460)

        self.folder = tk.StringVar(value=cfg.get("folder") or str(Path.home() / "Music" / "grabbed"))
        self.codec = tk.StringVar(value=cfg.get("format") or "mp3")
        self.quality = tk.StringVar(value=cfg.get("quality") or "192")
        self.browser = tk.StringVar(value=cfg.get("browser") or NO_COOKIES)
        self.url = tk.StringVar()
        self.stem = tk.StringVar()
        self.status = tk.StringVar(value="Paste a URL to start.")

        self._build(root)
        self._sync_quality_state()
        self.root.after(100, self._pump)

    # ------------------------------------------------------------------ layout

    def _build(self, root: tk.Tk) -> None:
        pad = {"padx": 8, "pady": 4}
        frame = ttk.Frame(root, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        # --- destination -------------------------------------------------
        ttk.Label(frame, text="Save to").grid(row=0, column=0, sticky="w", **pad)
        dest = ttk.Frame(frame)
        dest.grid(row=0, column=1, columnspan=2, sticky="ew", **pad)
        dest.columnconfigure(0, weight=1)
        ttk.Entry(dest, textvariable=self.folder).grid(row=0, column=0, sticky="ew")
        ttk.Button(dest, text="Browse...", command=self.browse).grid(row=0, column=1, padx=(6, 0))

        # --- format / quality / cookies ----------------------------------
        opts = ttk.Frame(frame)
        opts.grid(row=1, column=0, columnspan=3, sticky="ew", **pad)

        ttk.Label(opts, text="Format").grid(row=0, column=0, sticky="w")
        fmt = ttk.Combobox(opts, textvariable=self.codec, values=script.CODECS,
                           state="readonly", width=7)
        fmt.grid(row=0, column=1, padx=(6, 18))
        fmt.bind("<<ComboboxSelected>>", lambda _e: self._sync_quality_state())

        ttk.Label(opts, text="Quality").grid(row=0, column=2, sticky="w")
        self.quality_entry = ttk.Entry(opts, textvariable=self.quality, width=7)
        self.quality_entry.grid(row=0, column=3, padx=(6, 2))
        self.quality_hint = ttk.Label(opts, text="kbps", foreground="grey")
        self.quality_hint.grid(row=0, column=4, padx=(0, 18))

        ttk.Label(opts, text="Cookies from").grid(row=0, column=5, sticky="w")
        cook = ttk.Combobox(opts, textvariable=self.browser,
                            values=[NO_COOKIES] + script.BROWSERS, state="readonly", width=10)
        cook.grid(row=0, column=6, padx=(6, 0))
        cook.bind("<<ComboboxSelected>>", lambda _e: self.check_cookies())

        ttk.Separator(frame).grid(row=2, column=0, columnspan=3, sticky="ew", pady=10)

        # --- url ----------------------------------------------------------
        ttk.Label(frame, text="URL").grid(row=3, column=0, sticky="w", **pad)
        url_entry = ttk.Entry(frame, textvariable=self.url)
        url_entry.grid(row=3, column=1, sticky="ew", **pad)
        url_entry.bind("<Return>", lambda _e: self.fetch())
        self.fetch_btn = ttk.Button(frame, text="Fetch", command=self.fetch)
        self.fetch_btn.grid(row=3, column=2, **pad)

        ttk.Label(frame, text="Save as").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(frame, textvariable=self.stem).grid(row=4, column=1, sticky="ew", **pad)
        self.dl_btn = ttk.Button(frame, text="Download", command=self.start_download)
        self.dl_btn.grid(row=4, column=2, **pad)

        # --- progress + log ------------------------------------------------
        self.bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.bar.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Label(frame, textvariable=self.status, foreground="grey").grid(
            row=6, column=0, columnspan=3, sticky="w", **pad)

        # No hardcoded colours here. Tk's default Text background is
        # systemTextBackgroundColor, which follows the light/dark setting; a
        # fixed light grey renders as a glaring white slab in dark mode, and
        # systemTextColor text on it is invisible.
        log_frame = ttk.LabelFrame(frame, text="Log", padding=6)
        log_frame.grid(row=7, column=0, columnspan=3, sticky="nsew", **pad)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_box = tk.Text(log_frame, height=6, wrap="word", state="disabled",
                               relief="flat", highlightthickness=0)
        self.log_box.grid(row=0, column=0, sticky="nsew")
        frame.rowconfigure(7, weight=1)

    # ------------------------------------------------------------------ helpers

    def log(self, msg: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _sync_quality_state(self) -> None:
        lossless = self.codec.get() in LOSSLESS
        self.quality_entry.configure(state="disabled" if lossless else "normal")
        self.quality_hint.configure(text="lossless" if lossless else "kbps")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.fetch_btn.configure(state=state)
        self.dl_btn.configure(state=state)

    def post(self, kind: str, *payload) -> None:
        """Called from worker threads; the UI is only touched by _pump."""
        self.events.put((kind, *payload))

    def remember(self) -> None:
        script.save_config(folder=self.folder.get(), format=self.codec.get(),
                           quality=self.quality.get(), browser=self.browser.get())

    def cookies_opt(self):
        chosen = self.browser.get()
        return script.cookie_opt(None if chosen == NO_COOKIES else chosen)

    # ------------------------------------------------------------------ actions

    def browse(self) -> None:
        start = self.folder.get() or str(Path.home())
        chosen = filedialog.askdirectory(title="Choose a download folder",
                                         initialdir=start, mustexist=False)
        if chosen:
            self.folder.set(chosen)
            self.remember()

    def check_cookies(self) -> None:
        chosen = self.browser.get()
        self.remember()
        if chosen == NO_COOKIES:
            self.status.set("Not using cookies.")
            return
        self.status.set(f"Checking {chosen} cookies...")
        threading.Thread(target=lambda: self.post("cookies", chosen,
                                                  *script.cookie_problem(chosen)),
                         daemon=True).start()

    def fetch(self) -> None:
        url = self.url.get().strip()
        if self.busy:
            return
        if not url.startswith(("http://", "https://")):
            messagebox.showwarning("Audio Grabber", "That doesn't look like a URL.")
            return
        self._set_busy(True)
        self.status.set("Reading...")
        # Tk variables are main-thread only, so resolve cookies before handing off.
        threading.Thread(target=self._probe_worker, args=(url, self.cookies_opt()),
                         daemon=True).start()

    def _probe_worker(self, url: str, cookies) -> None:
        try:
            self.post("probed", url, script.probe(url, cookies))
        except Exception as e:                       # a worker crash must not hang the UI
            self.post("probed", url, None, str(e))

    def start_download(self) -> None:
        if self.busy:
            return
        url = self.url.get().strip()
        if not url.startswith(("http://", "https://")):
            messagebox.showwarning("Audio Grabber", "That doesn't look like a URL.")
            return

        out_dir = Path(self.folder.get()).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Audio Grabber", f"Can't use that folder:\n{e}")
            return

        stem = script.sanitize(self.stem.get()) if self.stem.get().strip() else None
        if not stem:
            messagebox.showinfo("Audio Grabber", "Fetch the URL first, or type a filename.")
            return

        codec = self.codec.get()
        stem = self._resolve_collision(out_dir, stem, codec)
        if stem is None:
            return

        # If Fetch already resolved this URL, reuse the exact track it found.
        target = url
        if self.info and self.probed_url == url:
            target = self.info.get("webpage_url") or url

        self.remember()
        self._set_busy(True)
        self.bar["value"] = 0
        self.status.set("Starting...")
        self.log(f"Downloading {stem}.{codec}")
        quality = self.quality.get() if codec not in LOSSLESS else "0"
        # Same rule as fetch(): read every Tk variable here, not in the worker.
        threading.Thread(target=self._download_worker,
                         args=(target, out_dir, stem, codec, quality, self.cookies_opt()),
                         daemon=True).start()

    def _resolve_collision(self, out_dir: Path, stem: str, ext: str) -> str | None:
        """Main-thread dialog: Tk widgets can't be touched from a worker."""
        if not (out_dir / f"{stem}.{ext}").exists():
            return stem
        answer = messagebox.askyesnocancel(
            "File exists",
            f"{stem}.{ext} already exists.\n\n"
            "Yes  - overwrite it\n"
            "No   - save alongside as a numbered copy\n"
            "Cancel - do nothing")
        if answer is None:
            self.status.set("Cancelled.")
            return None
        if answer:
            try:
                (out_dir / f"{stem}.{ext}").unlink()
            except OSError as e:
                messagebox.showerror("Audio Grabber", f"Couldn't replace it:\n{e}")
                return None
            return stem
        n = 2
        while (out_dir / f"{stem} ({n}).{ext}").exists():
            n += 1
        return f"{stem} ({n})"

    def _download_worker(self, url, out_dir, stem, codec, quality, cookies) -> None:
        def on_progress(d):
            if d.get("status") == "downloading":
                done = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                pct = (done / total * 100) if total else 0
                self.post("progress", pct,
                          f"{human(done)} of {human(total)}"
                          + (f" - {human(d.get('speed'))}/s" if d.get("speed") else ""))
            elif d.get("status") == "finished":
                self.post("progress", 100, "Download complete, converting...")

        def on_pp(d):
            if d.get("status") == "started":
                self.post("status", f"{d.get('postprocessor', 'Processing')}...")

        try:
            ok = script.download(url, out_dir, stem, codec, quality,
                                 cookies, on_progress, on_pp)
            self.post("done", ok, stem, codec)
        except Exception as e:
            self.post("done", False, stem, codec, str(e))

    # ------------------------------------------------------------------ ui pump

    def _pump(self) -> None:
        """Drain worker events on the main thread.

        Every handler is isolated: a raising handler must never stop the pump,
        because the whole UI goes silently dead if this loop stops rescheduling."""
        try:
            while True:
                try:
                    kind, *data = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    getattr(self, f"_on_{kind}")(*data)
                except Exception as e:
                    self.log(f"Internal error handling {kind!r}: {e}")
        finally:
            self.root.after(100, self._pump)

    def _on_cookies(self, chosen: str, fatal: str | None, note: str | None) -> None:
        if fatal:
            self.browser.set(NO_COOKIES)
            self.remember()
            self.status.set("Not using cookies.")
            messagebox.showwarning("Cookies unavailable",
                                   f"Can't use {chosen} cookies:\n\n{fatal}")
            return
        self.status.set(f"Using {chosen} cookies.")
        self.log(f"Cookies: {chosen}")
        if note:                         # advisory only -- cookies still get sent
            self.log(f"Note: {note}")

    def _on_probed(self, url: str, info: dict | None, error: str = "") -> None:
        self._set_busy(False)
        if info is None:
            self.status.set("Couldn't read that URL.")
            self.log(f"Failed: {error or 'see console for details'}")
            return
        self.info, self.probed_url = info, url
        self.stem.set(script.sanitize(info.get("title") or "untitled"))
        who = info.get("uploader") or info.get("channel") or ""
        secs = info.get("duration")
        length = f"  ({script.clock(secs)})" if secs else ""
        self.status.set(f"{who}{length}".strip() or "Ready.")
        self.log(f"Found: {info.get('title')}")
        if info.get("from_playlist"):
            self.log("Note: that's a playlist. Using the first track only.")

    def _on_progress(self, pct: float, text: str) -> None:
        self.bar["value"] = pct
        self.status.set(text)

    def _on_status(self, text: str) -> None:
        self.status.set(text)

    def _on_done(self, ok: bool, stem: str, codec: str, error: str = "") -> None:
        self._set_busy(False)
        self.bar["value"] = 100 if ok else 0
        if ok:
            self.status.set(f"Saved {stem}.{codec}")
            self.log(f"Saved: {stem}.{codec}")
            self.url.set("")
            self.stem.set("")
            self.info = self.probed_url = None
        else:
            self.status.set("Download failed.")
            self.log(f"Failed: {stem}.{codec} {error}")


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
