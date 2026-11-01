from __future__ import annotations

import asyncio


class LogicalSlotPool:
    """Logical slot allocator for a bounded async resource pool."""

    def __init__(self, capacity: int, name: str):
        if capacity < 1:
            raise ValueError(f"{name} capacity must be >= 1")

        self.capacity = capacity
        self.name = name
        self._available: asyncio.Queue[int] = asyncio.Queue()
        for slot_id in range(1, capacity + 1):
            self._available.put_nowait(slot_id)

        self._leased: set[int] = set()
        self._in_use = 0
        self._peak_in_use = 0
        self._lock = asyncio.Lock()

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def peak_in_use(self) -> int:
        return self._peak_in_use

    async def acquire(self) -> int:
        slot_id = await self._available.get()
        async with self._lock:
            self._leased.add(slot_id)
            self._in_use += 1
            self._peak_in_use = max(self._peak_in_use, self._in_use)
        return slot_id

    async def release(self, slot_id: int | None) -> None:
        if slot_id is None:
            return

        async with self._lock:
            if slot_id not in self._leased:
                return
            self._leased.remove(slot_id)
            self._in_use = max(0, self._in_use - 1)

        self._available.put_nowait(slot_id)
