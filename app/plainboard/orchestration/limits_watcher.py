"""Reads the plan limits of every subscription now and then, also when nobody looks.

A subscription reports its limits while an agent runs on it, and the UI asks for them while it is
open. One that is not in use, with the UI closed, reports nothing: what is shown for it, and what the
choice of an account goes by, would stay as it was hours ago (it may be in use outside PlainBoard,
and PlainBoard itself stops using a subscription that is nearly full, so that one is never read again
by a turn). Reading costs no tokens, or a handful for a Claude subscription signed in with a token.
"""

from __future__ import annotations

import asyncio
import logging

from .run_manager import RunManager

log = logging.getLogger(__name__)


class LimitsWatcher:
    def __init__(self, runs: RunManager, interval: float):
        self._runs = runs
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="limits-watcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)  # its database connection is back before the engine goes

    async def _loop(self) -> None:
        while True:
            # Sleep first: right after a start the UI's own request reads them.
            await asyncio.sleep(self._interval)
            try:
                self._runs.refresh_limits()
            except Exception:
                log.exception("Limits watcher tick failed")
