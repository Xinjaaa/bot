import threading
import time
from dataclasses import dataclass, field


@dataclass
class ConversationTurn:
    role: str
    content: str
    created_at: float


@dataclass
class ConversationState:
    updated_at: float
    turns: list[ConversationTurn] = field(default_factory=list)


class InMemoryConversationStore:
    def __init__(self, max_turns: int = 6, ttl_seconds: int = 1800) -> None:
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._items: dict[str, ConversationState] = {}

    def get_turns(self, user_id: str) -> list[ConversationTurn]:
        now = time.time()
        with self._lock:
            self._purge(now)
            state = self._items.get(user_id)
            if not state:
                return []
            state.updated_at = now
            return list(state.turns)

    def append_turn(self, user_id: str, role: str, content: str) -> None:
        now = time.time()
        with self._lock:
            self._purge(now)
            state = self._items.setdefault(user_id, ConversationState(updated_at=now))
            state.updated_at = now
            state.turns.append(ConversationTurn(role=role, content=content, created_at=now))
            max_items = max(self.max_turns * 2, 2)
            if len(state.turns) > max_items:
                state.turns = state.turns[-max_items:]

    def clear(self, user_id: str) -> None:
        with self._lock:
            self._items.pop(user_id, None)

    def _purge(self, now: float) -> None:
        expired = [user_id for user_id, state in self._items.items() if now - state.updated_at > self.ttl_seconds]
        for user_id in expired:
            self._items.pop(user_id, None)
