"""Bounded process-local limits for the single-process deployment."""
from collections import deque
from collections.abc import Callable
import time


class RateLimiter:
    def __init__(self, max_keys: int = 10000, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_keys = max_keys
        self.clock = clock
        self._entries: dict[str, tuple[deque[float], float]] = {}

    def hit(self, key: str, *, limit: int, window: float) -> bool:
        if limit < 1 or window <= 0:
            raise ValueError('Positive limit and window required')
        now = self.clock()
        if key not in self._entries:
            if len(self._entries) >= self.max_keys:
                expired = [k for k, (q, w) in self._entries.items() if not q or q[-1] + w <= now]
                for item in expired:
                    del self._entries[item]
                if len(self._entries) >= self.max_keys:
                    return False
            self._entries[key] = (deque(), window)
        queue, _ = self._entries[key]
        while queue and queue[0] <= now - window:
            queue.popleft()
        if len(queue) >= limit:
            return False
        queue.append(now)
        self._entries[key] = (queue, window)
        return True
