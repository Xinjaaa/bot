import threading
import time


class TTLMessageDeduper:
    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._items: dict[str, float] = {}

    def seen(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            self._purge(now)
            expires_at = self._items.get(key)
            if expires_at and expires_at > now:
                return True
            self._items[key] = now + self.ttl_seconds
            return False

    def _purge(self, now: float) -> None:
        expired = [key for key, expires_at in self._items.items() if expires_at <= now]
        for key in expired:
            self._items.pop(key, None)
