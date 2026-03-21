"""Tests for transport.py — Transport abstraction layer."""

import sys
import os
import json
import threading
import time

import pytest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "skills", "agent-fax", "scripts"
)
sys.path.insert(0, SCRIPTS_DIR)

from transport import LocalBus, LocalTransport, XMTPTransport, Transport


# ── LocalBus tests ────────────────────────────────────────────────


@pytest.mark.unit
def test_local_bus_register():
    """Register two agents, check agent_names()."""
    bus = LocalBus()
    bus.register("alice")
    bus.register("bob")
    names = bus.agent_names()
    assert "alice" in names
    assert "bob" in names
    assert len(names) == 2


@pytest.mark.unit
def test_local_bus_post_and_drain():
    """Post to agent, drain gets the messages."""
    bus = LocalBus()
    bus.register("alice")
    bus.post("alice", {"data": "hello"})
    bus.post("alice", {"data": "world"})
    msgs = bus.drain("alice")
    assert len(msgs) == 2
    assert msgs[0]["data"] == "hello"
    assert msgs[1]["data"] == "world"


@pytest.mark.unit
def test_local_bus_drain_empty():
    """Draining empty queue returns []."""
    bus = LocalBus()
    bus.register("alice")
    assert bus.drain("alice") == []
    # Draining unregistered agent also returns []
    assert bus.drain("nobody") == []


@pytest.mark.unit
def test_local_bus_thread_safety():
    """10 threads each post 100 messages, check total count."""
    bus = LocalBus()
    bus.register("target")

    def poster():
        for i in range(100):
            bus.post("target", {"i": i})

    threads = [threading.Thread(target=poster) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    msgs = bus.drain("target")
    assert len(msgs) == 1000


# ── LocalTransport tests ─────────────────────────────────────────


@pytest.mark.unit
def test_local_transport_send_receive():
    """Two agents on same bus, A sends to B, B.receive() gets it."""
    bus = LocalBus()
    alice = bus.register("alice")
    bob = bus.register("bob")

    alice.send("bob", "hello from alice")
    msgs = bob.receive()
    assert len(msgs) == 1
    assert msgs[0]["messages"][0]["content"] == "hello from alice"


@pytest.mark.unit
def test_local_transport_send_returns_ok():
    """send() returns {"ok": True, ...}."""
    bus = LocalBus()
    alice = bus.register("alice")
    bus.register("bob")

    result = alice.send("bob", "test")
    assert result["ok"] is True
    assert "id" in result


@pytest.mark.unit
def test_local_transport_health():
    """health() returns {"status": "ok", "transport": "local"}."""
    bus = LocalBus()
    alice = bus.register("alice")
    h = alice.health()
    assert h["status"] == "ok"
    assert h["transport"] == "local"


@pytest.mark.unit
def test_local_transport_loss_rate():
    """With loss_rate=1.0, no messages get through."""
    bus = LocalBus()
    bus.register("alice")
    bob = bus.register("bob")
    alice = LocalTransport("alice", bus, loss_rate=1.0)

    for _ in range(20):
        alice.send("bob", "should be lost")

    msgs = bob.receive()
    assert len(msgs) == 0


@pytest.mark.unit
def test_local_transport_duplicate():
    """With duplicate_rate=1.0, messages doubled."""
    bus = LocalBus()
    bus.register("alice")
    bob = bus.register("bob")
    alice = LocalTransport("alice", bus, duplicate_rate=1.0)

    alice.send("bob", "dup me")
    msgs = bob.receive()
    assert len(msgs) == 2


@pytest.mark.unit
def test_local_transport_delay():
    """With delay_ms=50, send takes >= 50ms."""
    bus = LocalBus()
    bus.register("alice")
    bus.register("bob")
    alice = LocalTransport("alice", bus, delay_ms=50)

    t0 = time.time()
    alice.send("bob", "slow")
    elapsed_ms = (time.time() - t0) * 1000
    assert elapsed_ms >= 45  # small tolerance


@pytest.mark.unit
def test_local_transport_message_format():
    """Received messages have valid AgentFax wrapper format."""
    bus = LocalBus()
    alice = bus.register("alice")
    bob = bus.register("bob")

    envelope = json.dumps({
        "protocol": "agentfax",
        "version": "1.0",
        "type": "ping",
        "payload": {},
        "timestamp": "2026-01-01T00:00:00+00:00",
        "ttl": 3600,
    })
    alice.send("bob", envelope)
    msgs = bob.receive()
    assert len(msgs) == 1
    inner = msgs[0]["messages"][0]
    assert "content" in inner
    assert "id" in inner
    assert "sentAt" in inner
    parsed = json.loads(inner["content"])
    assert parsed["protocol"] == "agentfax"


# ── Type hierarchy tests ─────────────────────────────────────────


@pytest.mark.unit
def test_xmtp_transport_is_transport():
    """XMTPTransport is a subclass of Transport."""
    assert issubclass(XMTPTransport, Transport)


@pytest.mark.unit
def test_local_transport_is_transport():
    """LocalTransport is a subclass of Transport."""
    assert issubclass(LocalTransport, Transport)


# ── AgentFaxClient integration tests ─────────────────────────────


@pytest.mark.unit
def test_agentfax_client_with_transport(tmp_data_dir):
    """AgentFaxClient can be constructed with a LocalTransport."""
    from agentfax_client import AgentFaxClient

    bus = LocalBus()
    transport = bus.register("test-agent")

    client = AgentFaxClient(tmp_data_dir, transport=transport)
    assert client._transport is transport


@pytest.mark.unit
def test_agentfax_client_send_via_transport(tmp_data_dir):
    """send() goes through transport, not bridge."""
    from agentfax_client import AgentFaxClient

    bus = LocalBus()
    sender_t = bus.register("sender")
    receiver_t = bus.register("receiver")

    client = AgentFaxClient(tmp_data_dir, transport=sender_t)
    result = client.send("receiver", "ping", {"message": "hi"})
    assert result["ok"] is True

    msgs = receiver_t.receive()
    assert len(msgs) == 1
    content = msgs[0]["messages"][0]["content"]
    parsed = json.loads(content)
    assert parsed["protocol"] == "agentfax"
    assert parsed["type"] == "ping"


@pytest.mark.unit
def test_agentfax_client_receive_via_transport(tmp_data_dir):
    """receive() parses messages from transport."""
    from agentfax_client import AgentFaxClient, build_message

    bus = LocalBus()
    client_t = bus.register("client")
    peer_t = bus.register("peer")

    client = AgentFaxClient(tmp_data_dir, transport=client_t)

    # Peer sends a message to client via bus
    envelope = build_message("ping", {"message": "hello"}, sender_id="peer")
    peer_t.send("client", json.dumps(envelope))

    msgs = client.receive()
    assert len(msgs) == 1
    assert msgs[0]["type"] == "ping"
    assert msgs[0]["payload"]["message"] == "hello"
