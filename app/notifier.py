"""Wakes long-polling readers the moment a message lands on their bus.

Exists so agents don't have to hand-roll a shell polling loop. Every agent that
writes its own loop gets a fresh opportunity to introduce a bug in the one
mechanism keeping it alive, and they do edit it.
"""

import asyncio
import logging

log = logging.getLogger("switchboard.notifier")


class Notifier:
    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}

    def _event(self, bus_id: str) -> asyncio.Event:
        event = self._events.get(bus_id)
        if event is None:
            event = self._events[bus_id] = asyncio.Event()
        return event

    def notify(self, bus_id: str) -> None:
        """Pulse every current waiter on this bus.

        set() resolves the futures of everyone already waiting; clear() only
        affects who arrives next, so an immediate clear cannot un-wake them.
        """
        event = self._event(bus_id)
        event.set()
        event.clear()

    async def wait(self, bus_id: str, timeout: float) -> bool:
        """True if woken, False if the timeout expired."""
        try:
            await asyncio.wait_for(self._event(bus_id).wait(), timeout=timeout)
            return True
        except (TimeoutError, asyncio.TimeoutError):
            return False
