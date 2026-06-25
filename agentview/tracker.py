from __future__ import annotations

import asyncio
import sys
import time
import weakref
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from typing import AsyncGenerator, Generator

from rich.console import Console
from rich.live import Live
from rich.table import Table

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

console = Console()

ICONS = {
    "pending": "[dim]○[/dim]",
    "running": "[bold yellow]▶[/bold yellow]",
    "done":    "[bold green]✓[/bold green]",
    "failed":  "[bold red]✗[/bold red]",
}


class Step:
    def __init__(self, name: str, _tracker: "Tracker | None" = None):
        self.name = name
        self.status: str = "pending"
        self.elapsed: float | None = None
        self._start: float | None = None
        self.sub_steps: list[Step] = []
        self.message: str | None = None
        self.progress: tuple[int, int] | None = None
        self._tracker_ref: weakref.ref | None = (
            weakref.ref(_tracker) if _tracker is not None else None
        )

    @property
    def _tracker(self) -> "Tracker | None":
        if self._tracker_ref is None:
            return None
        return self._tracker_ref()

    def start(self) -> None:
        self.status = "running"
        self._start = time.time()

    def finish(self) -> None:
        self.status = "done"
        self.elapsed = time.time() - (self._start if self._start is not None else time.time())

    def fail(self) -> None:
        self.status = "failed"
        self.elapsed = time.time() - (self._start if self._start is not None else time.time())

    def set_message(self, msg: str | None) -> None:
        self.message = msg
        tracker = self._tracker
        if tracker:
            tracker._refresh()

    def set_progress(self, current: int, total: int) -> None:
        self.progress = (current, total)
        tracker = self._tracker
        if tracker:
            tracker._refresh()

    @contextmanager
    def sub_step(self, name: str) -> Generator["Step", None, None]:
        tracker = self._tracker
        s = Step(name, _tracker=tracker)
        self.sub_steps.append(s)
        s.start()
        if tracker:
            tracker._refresh()
        try:
            yield s
            s.finish()
            if tracker:
                tracker._refresh()
        except Exception:
            s.fail()
            if tracker:
                try:
                    tracker._refresh()
                except Exception:
                    pass
            raise


class Tracker:
    def __init__(self):
        self._steps: list[Step] = []
        self._live: Live | None = None
        self._session_active: bool = False

    def _format_elapsed(self, s: Step) -> str:
        if s.elapsed is not None:
            return f"[dim]{s.elapsed:.1f}s[/dim]"
        elif s.status == "running":
            return "[dim]...[/dim]"
        return ""

    def _format_info(self, s: Step) -> str:
        if s.progress is not None:
            current, total = s.progress
            pct = current / total if total > 0 else 0
            filled = int(pct * 10)
            bar = "█" * filled + "░" * (10 - filled)
            return f"[dim]{current}/{total}[/dim] [green]{bar}[/green]"
        if s.message:
            return f"[dim]{s.message}[/dim]"
        return ""

    def _build_table(self) -> Table:
        table = Table.grid(padding=(0, 2))
        table.add_column(width=2)
        table.add_column(min_width=20)
        table.add_column(min_width=16)
        table.add_column(width=8, justify="right")
        for s in self._steps:
            table.add_row(ICONS[s.status], s.name, self._format_info(s), self._format_elapsed(s))
            for sub in s.sub_steps:
                table.add_row(
                    "",
                    f"  {ICONS[sub.status]}  {sub.name}",
                    self._format_info(sub),
                    self._format_elapsed(sub),
                )
        return table

    def _ensure_live(self) -> None:
        if self._live is None:
            self._live = Live(self._build_table(), console=console, refresh_per_second=10)
            self._live.start()

    def _refresh(self) -> None:
        if self._live:
            self._live.update(self._build_table())

    def _close_live(self) -> None:
        if self._live:
            self._live.update(self._build_table())
            live, self._live = self._live, None
            live.stop()

    @contextmanager
    def session(self):
        """Wrap the whole agent run to control Live lifecycle explicitly."""
        self._steps = []
        self._session_active = True
        self._ensure_live()
        try:
            yield self
            self._close_live()
        except Exception:
            self._close_live()
            raise
        finally:
            self._session_active = False

    def plan(self, names: list[str]) -> None:
        for name in names:
            if not any(s.name == name for s in self._steps):
                self._steps.append(Step(name, _tracker=self))
        self._ensure_live()
        self._refresh()

    def step(self, name: str) -> "_StepProxy":
        return _StepProxy(self, name)

    def _find_or_create(self, name: str) -> "Step":
        for s in self._steps:
            if s.name == name and s.status == "pending":
                return s
        s = Step(name, _tracker=self)
        self._steps.append(s)
        return s

    # ── sync internals ────────────────────────────────────────────────────

    def _run_step(self, name: str, func, args, kwargs):
        s = self._find_or_create(name)
        self._ensure_live()
        s.start()
        self._refresh()
        try:
            result = func(*args, **kwargs)
            s.finish()
            self._refresh()
            if not self._session_active:
                self._close_live()
            return result
        except Exception:
            s.fail()
            self._refresh()
            if not self._session_active:
                self._close_live()
            raise

    @contextmanager
    def _context_step(self, name: str) -> Generator[Step, None, None]:
        s = self._find_or_create(name)
        self._ensure_live()
        s.start()
        self._refresh()
        try:
            yield s
            s.finish()
            self._refresh()
            if not self._session_active:
                self._close_live()
        except Exception:
            s.fail()
            self._refresh()
            if not self._session_active:
                self._close_live()
            raise

    # ── async internals ───────────────────────────────────────────────────

    async def _async_run_step(self, name: str, func, args, kwargs):
        s = self._find_or_create(name)
        self._ensure_live()
        s.start()
        self._refresh()
        try:
            result = await func(*args, **kwargs)
            s.finish()
            self._refresh()
            if not self._session_active:
                self._close_live()
            return result
        except Exception:
            s.fail()
            self._refresh()
            if not self._session_active:
                self._close_live()
            raise

    @asynccontextmanager
    async def _async_context_step(self, name: str) -> AsyncGenerator[Step, None]:
        s = self._find_or_create(name)
        self._ensure_live()
        s.start()
        self._refresh()
        try:
            yield s
            s.finish()
            self._refresh()
            if not self._session_active:
                self._close_live()
        except Exception:
            s.fail()
            self._refresh()
            if not self._session_active:
                self._close_live()
            raise


class _StepProxy:
    def __init__(self, tracker: Tracker, name: str):
        self._tracker = tracker
        self._name = name

    def __call__(self, func):
        if asyncio.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                return await self._tracker._async_run_step(self._name, func, args, kwargs)
            return async_wrapper

        @wraps(func)
        def wrapper(*args, **kwargs):
            return self._tracker._run_step(self._name, func, args, kwargs)
        return wrapper

    # sync context manager
    def __enter__(self):
        self._ctx = self._tracker._context_step(self._name)
        return self._ctx.__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._ctx.__exit__(exc_type, exc_val, exc_tb)

    # async context manager
    async def __aenter__(self):
        self._async_ctx = self._tracker._async_context_step(self._name)
        return await self._async_ctx.__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self._async_ctx.__aexit__(exc_type, exc_val, exc_tb)


tracker = Tracker()
