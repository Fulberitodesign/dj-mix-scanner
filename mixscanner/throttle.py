"""Espacement des requêtes vers les services qui limitent le débit (Shazam, iTunes, MusicBrainz…)."""
from __future__ import annotations

import asyncio


class Throttle:
    """Garantit au moins `interval` secondes entre deux requêtes, même lancées en parallèle."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._next = 0.0
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def pause(self, seconds: float) -> None:
        """Repousse la prochaine requête (le service a signalé une limite de débit)."""
        if self._loop is not None:
            self._next = max(self._next, self._loop.time() + seconds)

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:  # verrou lié à la boucle en cours
            self._lock, self._loop, self._next = asyncio.Lock(), loop, 0.0
        async with self._lock:
            delay = self._next - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = max(self._next, loop.time()) + self.interval
