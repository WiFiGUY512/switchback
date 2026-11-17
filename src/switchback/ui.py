"""Console output: timestamped log lines and one live progress line under them."""

from __future__ import annotations

import asyncio
import shutil
import sys
import time


def clock(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


class Progress:
    """Which step, a bar, and time spent against the usual time for that step.

    Neither the plug nor the server reports a percentage for most steps, so the bar is time
    against the usual length of the step. A step that runs long keeps a full bar and a clock.
    """

    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self.step = self.label = self.note = ""
        self.expected = 1.0
        self.since = 0.0
        self.shown = 0

    def start(self) -> None:
        if sys.stdout.isatty() and self.task is None:
            self.task = asyncio.get_running_loop().create_task(self._run())

    def stop(self) -> None:
        if self.task:
            self.task.cancel()
            self.task = None
        self.step = ""
        self.clear()

    async def _run(self) -> None:
        while True:
            self.draw()
            await asyncio.sleep(0.5)

    def phase(self, step: str, label: str, expected: float) -> None:
        self.step, self.label, self.expected, self.note = step, label, max(expected, 1.0), ""
        self.since = time.monotonic()
        self.draw()

    def clear(self) -> None:
        if self.shown:
            sys.stdout.write("\r" + " " * self.shown + "\r")
            sys.stdout.flush()
            self.shown = 0

    def draw(self) -> None:
        if not self.task or not self.step:
            return
        elapsed = time.monotonic() - self.since
        columns = shutil.get_terminal_size((80, 24)).columns - 1
        head = f"{self.step} {self.label} ["
        tail = f"] {clock(elapsed)}/~{clock(self.expected)}" + (f" {self.note}" if self.note else "")
        width = max(8, min(30, columns - len(head) - len(tail)))
        filled = min(width, int(width * elapsed / self.expected))
        line = (head + "#" * filled + "-" * (width - filled) + tail)[:columns]
        sys.stdout.write("\r" + line + " " * max(0, self.shown - len(line)))
        sys.stdout.flush()
        self.shown = len(line)


PROGRESS = Progress()


def log(message: str) -> None:
    PROGRESS.clear()
    print(f"{time.strftime('%H:%M:%S')}  {message}", flush=True)
    PROGRESS.draw()
