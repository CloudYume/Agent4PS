from __future__ import annotations

import sys
import threading
import time
from collections import deque
from datetime import datetime

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.text import Text

from .types import ProgressEvent


_STEPS = {
    "select": 1, "solve": 2, "resume": 2, "local": 3, "review": 4,
    "run": 5, "prepare": 6, "submit": 7, "judge": 8,
    "archive": 9, "navigate": 10, "skip": 10,
    "search": 2, "repair": 2,
}


class TerminalUI:
    def __init__(self, console: Console | None = None):
        self.console = console or Console()
        self.interactive = self.console.is_terminal and sys.stdout.isatty()
        self.lock = threading.RLock()
        self.lines: deque[str] = deque(maxlen=18)
        self.number: int | None = None
        self.attempt = 1
        self.stage = "waiting"
        self.stage_started = time.monotonic()
        self.progress = Progress(
            SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=None),
            TextColumn("{task.completed:.0f}/10"), TimeElapsedColumn(),
            console=self.console, expand=True,
        )
        self.task = self.progress.add_task("Waiting for Edge", total=10, completed=0)
        self.live: Live | None = None

    def start(self) -> None:
        if self.interactive:
            self.live = Live(self._render(), console=self.console, screen=True, refresh_per_second=4)
            self.live.start()

    def update(self, event: ProgressEvent) -> None:
        with self.lock:
            now = time.monotonic()
            if self.number is not None and self.stage not in {"waiting", "done"} and (event.stage != self.stage or event.number != self.number):
                self._append(f"Problem {self.number}: {self.stage} took {now - self.stage_started:.1f}s")
            if event.stage != self.stage or event.number != self.number:
                self.stage_started = now
            if event.number != self.number:
                self.number = event.number
                self.progress.reset(self.task, completed=0)
            self.attempt = event.attempt
            self.stage = event.stage
            self.progress.update(
                self.task,
                description=f"Problem {event.number} | attempt {event.attempt} | {event.stage}",
                completed=_STEPS.get(event.stage, 0),
            )
            self._append(event.message)

    def note(self, message: str) -> None:
        with self.lock:
            if (message.startswith("Problem ") or message.startswith("Stopped:")) and self.number is not None and self.stage not in {"waiting", "done"}:
                self._append(f"Problem {self.number}: {self.stage} took {time.monotonic() - self.stage_started:.1f}s")
                self.stage = "done"
            self._append(message)

    def close(self) -> None:
        with self.lock:
            if self.live:
                self.live.stop()
                self.live = None
                for line in self.lines:
                    self.console.print(line)

    def _append(self, message: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')}  {message}"
        self.lines.append(line)
        if self.live:
            self.live.update(self._render())
        else:
            self.console.print(line)

    def _render(self) -> Group:
        header = f"Agent4PS | Problem {self.number} | attempt {self.attempt}" if self.number else "Agent4PS | Waiting"
        details = Text("\n".join(self.lines) or "Waiting for the active LeetCode tab")
        return Group(
            Panel(self.progress, title=header, border_style="cyan"),
            Panel(details, title="Detailed progress", border_style="white"),
        )
