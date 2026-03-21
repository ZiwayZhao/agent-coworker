"""Transport abstraction for AgentFax messaging."""

import abc
import collections
import queue
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional


class Transport(abc.ABC):
    """Abstract base class for AgentFax transports."""

    @abc.abstractmethod
    def send(self, to_wallet: str, content: str) -> dict:
        ...

    @abc.abstractmethod
    def receive(self) -> list:
        ...

    @abc.abstractmethod
    def health(self) -> dict:
        ...


class XMTPTransport(Transport):
    """Wraps the XMTP bridge HTTP API (production transport)."""

    def __init__(self, data_dir: str):
        from .client import _bridge_get, _bridge_post
        self.data_dir = data_dir
        self._bridge_get = _bridge_get
        self._bridge_post = _bridge_post

    def send(self, to_wallet: str, content: str) -> dict:
        return self._bridge_post(self.data_dir, "/send", {
            "to": to_wallet,
            "content": content,
        })

    def receive(self) -> list:
        return self._bridge_get(self.data_dir, "/inbox")

    def health(self) -> dict:
        return self._bridge_get(self.data_dir, "/health")


class LocalBus:
    """Shared in-memory message bus for LocalTransports. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._queues: dict[str, collections.deque] = {}

    def register(self, agent_name: str) -> "LocalTransport":
        """Register an agent and return a LocalTransport bound to it."""
        with self._lock:
            if agent_name not in self._queues:
                self._queues[agent_name] = collections.deque()
        return LocalTransport(agent_name, self)

    def post(self, to_agent: str, message: dict):
        """Post a message to an agent's queue."""
        with self._lock:
            if to_agent not in self._queues:
                self._queues[to_agent] = collections.deque()
            self._queues[to_agent].append(message)

    def drain(self, agent_name: str) -> list:
        """Drain and return all pending messages for an agent."""
        with self._lock:
            q = self._queues.get(agent_name)
            if not q:
                return []
            msgs = list(q)
            q.clear()
            return msgs

    def clear(self):
        """Clear all queues."""
        with self._lock:
            for q in self._queues.values():
                q.clear()

    def agent_names(self) -> list:
        """Return list of registered agent names."""
        with self._lock:
            return list(self._queues.keys())


class LocalTransport(Transport):
    """In-memory message bus for testing. Thread-safe."""

    def __init__(
        self,
        agent_name: str,
        bus: "LocalBus",
        delay_ms: float = 0,
        loss_rate: float = 0.0,
        duplicate_rate: float = 0.0,
        reorder: bool = False,
    ):
        self.agent_name = agent_name
        self.bus = bus
        self.delay_ms = delay_ms
        self.loss_rate = loss_rate
        self.duplicate_rate = duplicate_rate
        self.reorder = reorder

    def send(self, to_wallet: str, content: str) -> dict:
        """Send a message. to_wallet is agent name in local mode."""
        if self.delay_ms > 0:
            time.sleep(self.delay_ms / 1000)

        msg_id = uuid.uuid4().hex
        message = {
            "messages": [{
                "content": content,
                "id": msg_id,
                "sentAt": datetime.now(timezone.utc).isoformat(),
            }]
        }

        if random.random() < self.loss_rate:
            return {"ok": True, "id": msg_id, "dropped": True}

        self.bus.post(to_wallet, message)

        if random.random() < self.duplicate_rate:
            self.bus.post(to_wallet, message)

        return {"ok": True, "id": msg_id}

    def receive(self) -> list:
        """Receive all pending messages."""
        msgs = self.bus.drain(self.agent_name)
        if self.reorder and len(msgs) > 1:
            random.shuffle(msgs)
        return msgs

    def health(self) -> dict:
        """Return health status."""
        return {"status": "ok", "transport": "local"}
