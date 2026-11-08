from collections import OrderedDict
import threading
import time
from uuid import uuid4

from .errors import XiaoNuoError


class ConversationStore:
    """Bounded 30-minute RAM context, with explicitly enabled retained storage."""

    def __init__(self, store=None):
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()
        self.store = store

    def respond(self, conversation_id: str | None, text: str, generate):
        # P1 is a single-owner, half-duplex service. Serialization preserves turn order.
        if not self._lock.acquire(timeout=1):
            raise XiaoNuoError("CONVERSATION_BUSY", 429)
        try:
            moment = time.monotonic()
            self._sessions = OrderedDict((k, v) for k, v in self._sessions.items() if moment - v[0] < 1800)
            identifier = conversation_id or str(uuid4())
            initial = self.store.load_history(identifier) if self.store and identifier not in self._sessions else []
            history = list(self._sessions.get(identifier, (moment, initial))[1])
            messages = history + [{"role": "user", "content": text}]
            while len(messages) > 1 and sum(len(m["content"].encode("utf-8")) for m in messages) > 16000:
                messages = messages[2:]
            answer = generate(messages)
            completed = messages + [{"role": "assistant", "content": answer.completion.text}]
            self._sessions[identifier] = (time.monotonic(), completed[-12:])
            self._sessions.move_to_end(identifier)
            if self.store:
                self.store.save_history(identifier, completed)
            while len(self._sessions) > 64:
                self._sessions.popitem(last=False)
            return identifier, answer
        finally:
            self._lock.release()

    def delete(self, identifier: str):
        with self._lock:
            self._sessions.pop(identifier, None)
            if self.store:
                self.store.delete_history(identifier)

    def clear(self):
        with self._lock:
            self._sessions.clear()
