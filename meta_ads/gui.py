"""Desktop window for non-technical users (packaged as MetaAdsScraper.exe)."""

import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from meta_ads.api import Filters
from meta_ads.config import DEFAULT_API_VERSION
from meta_ads.runner import RunResult, new_run_dir, run_scrape

if sys.platform == "win32":
    SETTINGS_PATH = Path(os.environ.get("APPDATA", Path.home())) / "MetaAdsScraper" / "settings.json"
else:
    SETTINGS_PATH = Path.home() / "Library" / "Application Support" / "MetaAdsScraper" / "settings.json"
DEFAULT_OUT = Path.home() / "Documents" / "MetaAdsScraper"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

log = logging.getLogger("meta_ads")


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.replace(";", ",").split(",") if v.strip()]


def friendly_error(message: str) -> str:
    if "code 190" in message:
        return (
            "The access token is invalid or expired.\n\n"
            "Generate a new User token in Graph API Explorer (use the copy button), "
            "check it in the Access Token Debugger, and paste it again."
        )
    if "code 10" in message or "code 200" in message:
        return "This token has no Ad Library access. Confirm your identity at facebook.com/ID and use a token from that account."
    return message


class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.q.put(("log", self.format(record)))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Meta Ad Library Scraper")
        self.minsize(640, 620)
        self.events: queue.Queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_run_dir: Path | None = None

        log.setLevel(logging.INFO)
        log.addHandler(QueueLogHandler(self.events))

        self.vars = {
            "token": tk.StringVar(),
            "search_terms": tk.StringVar(),
            "page_ids": tk.StringVar(),
            "countries": tk.StringVar(value="PL"),
            "languages": tk.StringVar(),
            "status": tk.StringVar(value="ALL"),
            "media_type": tk.StringVar(value="ALL"),
            "date_min": tk.StringVar(),
            "date_max": tk.StringVar(),
            "max_ads": tk.StringVar(),
            "out_dir": tk.StringVar(value=str(DEFAULT_OUT)),
            "download_media": tk.BooleanVar(value=True),
        }
        self._load_settings()
        self._build()
        self.after(100, self._poll)

    # ---------- layout ----------
    def _build(self):
        form = ttk.Frame(self, padding=12)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        row = 0

        def add(label, widget, hint=""):
            nonlocal row
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 8))
            widget.grid(row=row, column=1, sticky="ew", pady=3)
            if hint:
                ttk.Label(form, text=hint, foreground="gray").grid(row=row, column=2, sticky="w", padx=(8, 0))
            row += 1

        token_row = ttk.Frame(form)
        token_entry = ttk.Entry(token_row, textvariable=self.vars["token"], show="•")
        token_entry.pack(side="left", fill="x", expand=True)
        show = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            token_row, text="show", variable=show, command=lambda: token_entry.config(show="" if show.get() else "•")
        ).pack(side="left", padx=(6, 0))
        add("Access token", token_row, "optional")

        add("Search terms", ttk.Entry(form, textvariable=self.vars["search_terms"]), "or page IDs below")
        add("Page IDs", ttk.Entry(form, textvariable=self.vars["page_ids"]), "comma-separated, max 10")
        add("Countries", ttk.Entry(form, textvariable=self.vars["countries"]), "e.g. PL,DE (required)")
        add("Languages", ttk.Entry(form, textvariable=self.vars["languages"]), "e.g. pl,en (optional)")
        add("Status", ttk.Combobox(form, textvariable=self.vars["status"], values=["ALL", "ACTIVE", "INACTIVE"], state="readonly"))
        add("Media type", ttk.Combobox(form, textvariable=self.vars["media_type"], values=["ALL", "IMAGE", "MEME", "VIDEO", "NONE"], state="readonly"))
        add("Delivered from", ttk.Entry(form, textvariable=self.vars["date_min"]), "YYYY-MM-DD (optional)")
        add("Delivered to", ttk.Entry(form, textvariable=self.vars["date_max"]), "YYYY-MM-DD (optional)")
        add("Max ads", ttk.Entry(form, textvariable=self.vars["max_ads"]), "empty = all")

        out_row = ttk.Frame(form)
        ttk.Entry(out_row, textvariable=self.vars["out_dir"]).pack(side="left", fill="x", expand=True)
        ttk.Button(out_row, text="Browse…", command=self._browse).pack(side="left", padx=(6, 0))
        add("Save to", out_row)
        add("", ttk.Checkbutton(form, text="Download images and videos", variable=self.vars["download_media"]))

        buttons = ttk.Frame(self, padding=(12, 0))
        buttons.pack(fill="x")
        self.start_btn = ttk.Button(buttons, text="Start", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(buttons, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.open_btn = ttk.Button(buttons, text="Open results folder", command=self._open_results, state="disabled")
        self.open_btn.pack(side="left")

        status = ttk.Frame(self, padding=(12, 8))
        status.pack(fill="x")
        self.progress = ttk.Progressbar(status, mode="indeterminate")
        self.progress.pack(fill="x")
        self.status = ttk.Label(status, text="Ready")
        self.status.pack(anchor="w", pady=(4, 0))

        self.log_box = ScrolledText(self, height=12, state="disabled", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    # ---------- actions ----------
    def _browse(self):
        path = filedialog.askdirectory(initialdir=self.vars["out_dir"].get() or str(Path.home()))
        if path:
            self.vars["out_dir"].set(path)

    def _collect(self) -> dict | None:
        v = {k: var.get() for k, var in self.vars.items()}
        v = {k: x.strip() if isinstance(x, str) else x for k, x in v.items()}
        for key in ("date_min", "date_max"):
            if v[key] and not DATE_RE.match(v[key]):
                messagebox.showerror("Invalid date", f"Use YYYY-MM-DD, got: {v[key]}")
                return None
        if v["max_ads"] and not v["max_ads"].isdigit():
            messagebox.showerror("Invalid number", "Max ads must be a whole number (or empty).")
            return None
        v["filters"] = Filters(
            countries=_split(v["countries"]),
            search_terms=v["search_terms"] or None,
            search_page_ids=_split(v["page_ids"]) or None,
            active_status=v["status"],
            date_min=v["date_min"] or None,
            date_max=v["date_max"] or None,
            languages=_split(v["languages"]) or None,
            media_type=v["media_type"],
        )
        try:
            v["filters"].validate()
        except ValueError as e:
            messagebox.showerror("Check the form", str(e).replace("_", " ").capitalize())
            return None
        return v

    def _start(self):
        v = self._collect()
        if not v:
            return
        self._save_settings()
        self.last_run_dir = new_run_dir(Path(v["out_dir"]))
        self.stop_flag.clear()
        self._clear_log()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.open_btn.config(state="normal")
        self.progress.start(12)
        self.status.config(text="Fetching ads…" if v["token"] else "Starting browser and searching…")
        log.info("Saving to %s", self.last_run_dir)

        def work():
            try:
                result = run_scrape(
                    token=v["token"] or None,
                    api_version=DEFAULT_API_VERSION,
                    filters=v["filters"],
                    run_dir=self.last_run_dir,
                    max_ads=int(v["max_ads"]) if v["max_ads"] else None,
                    skip_media=not v["download_media"],
                    on_ad=lambda r: self.events.put(("progress", r)),
                    should_stop=self.stop_flag.is_set,
                )
                self.events.put(("done", result))
            except Exception as e:  # unexpected; keep the window alive
                log.exception("Unexpected error")
                self.events.put(("crash", str(e)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _stop(self):
        self.stop_flag.set()
        self.stop_btn.config(state="disabled")
        self.status.config(text="Stopping after the current ad…")

    def _open_results(self):
        path = self.last_run_dir if self.last_run_dir and self.last_run_dir.exists() else Path(self.vars["out_dir"].get())
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", path])
        else:
            subprocess.run(["xdg-open", path])

    # ---------- worker → UI ----------
    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "progress":
                    self._show_progress(payload, running=True)
                elif kind == "done":
                    self._finish(payload)
                elif kind == "crash":
                    self._finish(None, crash=payload)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _show_progress(self, r: RunResult, running: bool):
        text = f"{r.processed} ads, {r.media_count} files downloaded"
        if r.failed:
            text += f", {r.failed} ads with media errors"
        self.status.config(text=("Working… " if running else "") + text)

    def _finish(self, result: RunResult | None, crash: str | None = None):
        self.progress.stop()
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        if crash:
            self.status.config(text="Failed")
            messagebox.showerror("Error", crash)
            return
        self._show_progress(result, running=False)
        if result.api_error:
            messagebox.showerror("Meta API error", friendly_error(result.api_error))
        elif result.stopped:
            self.status.config(text="Stopped. " + self.status.cget("text"))
        elif result.processed == 0:
            messagebox.showinfo("No ads", "No ads matched these filters.")
        else:
            log.info("Done. Results: %s", result.csv_path)

    def _append_log(self, line: str):
        self.log_box.config(state="normal")
        self.log_box.insert("end", line + "\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def _clear_log(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")

    # ---------- settings ----------
    def _load_settings(self):
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for k, var in self.vars.items():
            if k in data:
                var.set(data[k])

    def _save_settings(self):
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(json.dumps({k: var.get() for k, var in self.vars.items()}, indent=2), encoding="utf-8")
        except OSError:
            pass


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
