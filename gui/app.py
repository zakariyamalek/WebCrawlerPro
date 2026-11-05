"""CustomTkinter dashboard for WebCrawler Pro (``python main.py --gui``)."""
from __future__ import annotations

import queue
import tkinter as tk
from tkinter import filedialog, messagebox

try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise SystemExit("The GUI needs CustomTkinter:  pip install customtkinter") from exc

from config import APP_FULL_NAME, DEFAULT_OUTPUT_DIR, CrawlConfig
from gui.controller import CrawlController
from utils.helpers import human_duration
from utils.validators import validate_start_url

NOTICE = ("Only crawl websites you own or have explicit permission to crawl. WebCrawler Pro respects robots.txt, "
          "never logs in, submits forms or bypasses CAPTCHAs/paywalls.")


class CrawlerApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("WebCrawler Pro")
        self.geometry("1080x760")
        self.minsize(960, 680)
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")
        self.events: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.controller = CrawlController(lambda p: self.events.put(("progress", p)),
                                          lambda t: self.events.put(("log", t)),
                                          lambda rc: self.events.put(("exit", rc)))
        self.paused = False
        self.vars = {k: tk.BooleanVar(value=v) for k, v in {
            "include_subdomains": False, "extract_tables": True, "extract_metadata": True,
            "extract_external_links": True, "extract_images": True, "download_images": False}.items()}
        self._build()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # --------------------------------------------------------------- layout
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        d = CrawlConfig()
        ctk.CTkLabel(self, text=APP_FULL_NAME, font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, padx=16, pady=(12, 0), sticky="w")
        ctk.CTkLabel(self, text=NOTICE, wraplength=1000, justify="left", text_color=("gray30", "gray70")).grid(
            row=1, column=0, padx=16, pady=(0, 6), sticky="w")

        top = ctk.CTkFrame(self)
        top.grid(row=2, column=0, padx=12, pady=4, sticky="ew")
        top.columnconfigure(1, weight=1)
        ctk.CTkLabel(top, text="Website URL").grid(row=0, column=0, padx=8, pady=6)
        self.url = ctk.CTkEntry(top, placeholder_text="https://example.com")
        self.url.grid(row=0, column=1, columnspan=5, padx=8, pady=6, sticky="ew")

        def field(label: str, default, row: int, col: int) -> ctk.CTkEntry:
            ctk.CTkLabel(top, text=label).grid(row=row, column=col, padx=(8, 2), pady=4, sticky="e")
            e = ctk.CTkEntry(top, width=80)
            e.insert(0, str(default))
            e.grid(row=row, column=col + 1, padx=(0, 8), pady=4, sticky="w")
            return e

        self.depth = field("Max depth", d.max_depth, 1, 0)
        self.max_pages = field("Max pages", d.max_pages, 1, 2)
        self.concurrency = field("Concurrency", d.concurrency, 1, 4)
        self.delay = field("Delay (s)", d.delay, 2, 0)
        self.timeout = field("Timeout (s)", int(d.timeout), 2, 2)
        ctk.CTkLabel(top, text="JavaScript").grid(row=2, column=4, padx=(8, 2), sticky="e")
        self.js = ctk.CTkOptionMenu(top, values=["auto", "always", "never"], width=90)
        self.js.grid(row=2, column=5, padx=(0, 8), sticky="w")
        ctk.CTkLabel(top, text="Output").grid(row=3, column=0, padx=(8, 2), sticky="e")
        self.fmt = ctk.CTkOptionMenu(top, values=["both", "csv", "xlsx"], width=90)
        self.fmt.grid(row=3, column=1, padx=(0, 8), sticky="w")
        self.outdir = ctk.CTkEntry(top, width=300)
        self.outdir.insert(0, str(DEFAULT_OUTPUT_DIR))
        self.outdir.grid(row=3, column=2, columnspan=3, padx=4, pady=4, sticky="ew")
        ctk.CTkButton(top, text="Browse…", width=80, command=self._browse).grid(row=3, column=5, padx=8)
        opts = ctk.CTkFrame(top, fg_color="transparent")
        opts.grid(row=4, column=0, columnspan=6, sticky="w", padx=8)
        for key, label in (("include_subdomains", "Subdomains"), ("extract_tables", "Tables"),
                           ("extract_metadata", "Metadata"), ("extract_external_links", "External links"),
                           ("extract_images", "Image info"), ("download_images", "Download images")):
            ctk.CTkCheckBox(opts, text=label, variable=self.vars[key]).pack(side="left", padx=6, pady=4)
        self.auth = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(top, text="I am authorised to crawl this website", variable=self.auth).grid(
            row=5, column=0, columnspan=4, padx=8, pady=(2, 8), sticky="w")
        buttons = ctk.CTkFrame(top, fg_color="transparent")
        buttons.grid(row=5, column=4, columnspan=2, sticky="e", padx=8)
        self.start_btn = ctk.CTkButton(buttons, text="Start Crawling", command=self._start, width=120)
        self.pause_btn = ctk.CTkButton(buttons, text="Pause", command=self._toggle_pause, width=80, state="disabled")
        self.stop_btn = ctk.CTkButton(buttons, text="Stop", command=self._stop, width=70, state="disabled",
                                      fg_color="#b3261e", hover_color="#8c1d18")
        for b in (self.start_btn, self.pause_btn, self.stop_btn):
            b.pack(side="left", padx=3)

        body = ctk.CTkFrame(self)
        body.grid(row=3, column=0, padx=12, pady=6, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(3, weight=1)
        stats = ctk.CTkFrame(body)
        stats.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        self.stat: dict[str, ctk.CTkLabel] = {}
        names = ["URLs discovered", "URLs queued", "Pages crawled", "Pages remaining", "Successful",
                 "Failed", "Current depth", "Elapsed"]
        for i, name in enumerate(names):
            stats.columnconfigure(i % 4, weight=1)
            cell = ctk.CTkFrame(stats, fg_color="transparent")
            cell.grid(row=i // 4, column=i % 4, padx=8, pady=4, sticky="w")
            ctk.CTkLabel(cell, text=name, text_color=("gray35", "gray65")).pack(anchor="w")
            self.stat[name] = ctk.CTkLabel(cell, text="0", font=ctk.CTkFont(size=18, weight="bold"))
            self.stat[name].pack(anchor="w")
        self.current = ctk.CTkLabel(body, text="Current URL: -", anchor="w")
        self.current.grid(row=1, column=0, sticky="ew", padx=10)
        prog = ctk.CTkFrame(body, fg_color="transparent")
        prog.grid(row=2, column=0, sticky="ew", padx=10, pady=4)
        prog.columnconfigure(0, weight=1)
        self.bar = ctk.CTkProgressBar(prog)
        self.bar.set(0)
        self.bar.grid(row=0, column=0, sticky="ew")
        self.bar_label = ctk.CTkLabel(prog, text="Estimated progress (grows as new links are found; not a completion guarantee)")
        self.bar_label.grid(row=1, column=0, sticky="w")
        self.log = ctk.CTkTextbox(body, wrap="none", font=ctk.CTkFont(family="Courier", size=12))
        self.log.grid(row=3, column=0, sticky="nsew", padx=6, pady=6)
        self.log.configure(state="disabled")
        self.log.tag_config("warn", foreground="#d98e04") if hasattr(self.log, "tag_config") else None

    # --------------------------------------------------------------- actions
    def _browse(self) -> None:
        path = filedialog.askdirectory(initialdir=self.outdir.get() or str(DEFAULT_OUTPUT_DIR))
        if path:
            self.outdir.delete(0, "end")
            self.outdir.insert(0, path)

    def _settings(self) -> dict:
        url, check = validate_start_url(self.url.get())
        if not check.ok:
            raise ValueError(f"Invalid URL: {check.reason}")
        s = {"url": url, "max_depth": int(self.depth.get()), "max_pages": int(self.max_pages.get()),
             "concurrency": int(self.concurrency.get()), "delay": float(self.delay.get()),
             "timeout": float(self.timeout.get()), "javascript": self.js.get(), "output_format": self.fmt.get(),
             "output_dir": self.outdir.get().strip(), "respect_robots": True}
        s.update({k: v.get() for k, v in self.vars.items()})
        CrawlConfig.from_dict({**s}).validate()
        return s

    def _start(self) -> None:
        if not self.auth.get():
            messagebox.showwarning("Authorization required",
                                   "Please confirm that you are authorised to crawl this website.")
            return
        try:
            settings = self._settings()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return
        self._log("[INFO] Starting crawler.")
        self.controller.start(settings)
        self.paused = False
        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal", text="Pause")
        self.stop_btn.configure(state="normal")

    def _toggle_pause(self) -> None:
        if self.paused:
            self.controller.resume()
        else:
            self.controller.pause()
        self.paused = not self.paused
        self.pause_btn.configure(text="Resume" if self.paused else "Pause")

    def _stop(self) -> None:
        self._log("[INFO] Stop requested - finishing in-flight requests…")
        self.controller.stop()
        self.stop_btn.configure(state="disabled")

    def _on_close(self) -> None:
        if self.controller.running:
            if not messagebox.askyesno("Crawl running", "A crawl is running. Stop it and exit?"):
                return
            self.controller.stop()
        self.destroy()

    # ------------------------------------------------------------- event loop
    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _update_progress(self, p: dict) -> None:
        m = {"URLs discovered": p["discovered"], "URLs queued": p["queued"], "Pages crawled": p["pages_crawled"],
             "Pages remaining": p["pages_remaining"], "Successful": p["success"], "Failed": p["failed"],
             "Current depth": p["current_depth"], "Elapsed": human_duration(p["elapsed"])}
        for k, v in m.items():
            self.stat[k].configure(text=str(v))
        self.current.configure(text=f"Current URL: {p['current_url'] or '-'}" + ("   [PAUSED]" if p.get("paused") else ""))
        self.bar.set(p["progress_estimate"])

    def _poll(self) -> None:
        try:
            for _ in range(200):
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self._update_progress(payload)  # type: ignore[arg-type]
                elif kind == "log":
                    self._log(str(payload))
                elif kind == "exit":
                    self._log(f"[INFO] Crawler process finished (exit code {payload}).")
                    self.start_btn.configure(state="normal")
                    self.pause_btn.configure(state="disabled", text="Pause")
                    self.stop_btn.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll)


def launch_gui() -> int:
    CrawlerApp().mainloop()
    return 0
