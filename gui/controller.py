"""GUI back-end: runs each crawl in a subprocess and relays progress.

A subprocess is used because Twisted's reactor cannot be restarted and because it
keeps the Tk event loop completely free, so the window never freezes.  Commands
(``pause``/``resume``/``stop``) are sent over the child's stdin; progress arrives as
``@@PROGRESS {json}`` lines on its stdout and everything else is the activity log.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent


def build_command(s: dict[str, Any]) -> list[str]:
    """Translate GUI settings into a ``main.py`` command line."""
    cmd = [sys.executable, str(ROOT / "main.py"), "--url", s["url"], "--accept-authorization",
           "--json-progress", "--control-stdin",
           "--depth", str(s["max_depth"]), "--max-pages", str(s["max_pages"]),
           "--concurrency", str(s["concurrency"]), "--delay", str(s["delay"]), "--timeout", str(s["timeout"]),
           "--javascript", s["javascript"], "--format", s["output_format"]]
    for flag, key in (("subdomains", "include_subdomains"), ("tables", "extract_tables"),
                      ("metadata", "extract_metadata"), ("external-links", "extract_external_links"),
                      ("images", "extract_images"), ("download-images", "download_images"),
                      ("robots", "respect_robots")):
        cmd.append(f"--{flag}" if s.get(key, False) else f"--no-{flag}")
    if s.get("output_dir"):
        cmd += ["--output-dir", s["output_dir"]]
    if s.get("allow_private_network"):
        cmd.append("--allow-private-network")
    return cmd


def parse_line(line: str) -> tuple[str, Any]:
    """Return ('progress', dict) or ('log', text)."""
    line = line.rstrip("\r\n")
    if line.startswith("@@PROGRESS "):
        try:
            return "progress", json.loads(line[len("@@PROGRESS "):])
        except ValueError:
            pass
    return "log", line


class CrawlController:
    def __init__(self, on_progress: Callable[[dict], None], on_log: Callable[[str], None],
                 on_exit: Callable[[int], None]) -> None:
        self.on_progress, self.on_log, self.on_exit = on_progress, on_log, on_exit
        self.proc: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, settings: dict[str, Any]) -> None:
        if self.running:
            raise RuntimeError("A crawl is already running")
        self.proc = subprocess.Popen(build_command(settings), cwd=ROOT, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            kind, payload = parse_line(line)
            (self.on_progress if kind == "progress" else self.on_log)(payload)
        self.on_exit(proc.wait())

    def _send(self, command: str) -> None:
        if self.running and self.proc and self.proc.stdin:
            try:
                self.proc.stdin.write(command + "\n")
                self.proc.stdin.flush()
            except OSError:
                pass

    def pause(self) -> None:
        self._send("pause")

    def resume(self) -> None:
        self._send("resume")

    def stop(self) -> None:
        """Graceful stop; escalates to terminate() if the child does not exit in 20 s."""
        self._send("stop")
        proc = self.proc
        if proc is not None:
            threading.Timer(20.0, lambda: proc.poll() is None and proc.terminate()).start()
