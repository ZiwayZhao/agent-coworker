"""Shared fixtures for AgentFax tests."""

import os
import sys
import tempfile
import shutil
import pytest

# Add scripts dir to path so we can import modules directly
SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "skills", "agent-fax", "scripts"
)
sys.path.insert(0, SCRIPTS_DIR)


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Create a temporary data directory mimicking ~/.agentfax."""
    data_dir = str(tmp_path / "agentfax")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


@pytest.fixture
def sample_message():
    """A valid AgentFax protocol message."""
    from datetime import datetime, timezone

    return {
        "protocol": "agentfax",
        "version": "1.0",
        "type": "ping",
        "sender_id": "test_peer",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "correlation_id": "corr_001",
        "ttl": 3600,
        "payload": {"message": "hello"},
        "_xmtp_sender_wallet": "0xTEST_WALLET",
        "_xmtp_id": "msg_001",
    }


@pytest.fixture
def sample_task_request():
    """A valid task_request message."""
    from datetime import datetime, timezone

    return {
        "protocol": "agentfax",
        "version": "1.0",
        "type": "task_request",
        "sender_id": "test_peer",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "correlation_id": "corr_task_001",
        "ttl": 3600,
        "payload": {
            "task_id": "task_001",
            "skill": "echo",
            "input": {"text": "hello world"},
        },
        "_xmtp_sender_wallet": "0xTASK_WALLET",
        "_xmtp_id": "msg_task_001",
    }


@pytest.fixture
def make_message():
    """Factory fixture for creating messages with custom fields."""
    from datetime import datetime, timezone

    def _make(msg_type="ping", sender_id="test_peer", payload=None,
              ttl=3600, correlation_id=None, wallet="0xTEST"):
        return {
            "protocol": "agentfax",
            "version": "1.0",
            "type": msg_type,
            "sender_id": sender_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "correlation_id": correlation_id or f"corr_{msg_type}",
            "ttl": ttl,
            "payload": payload or {},
            "_xmtp_sender_wallet": wallet,
            "_xmtp_id": f"msg_{msg_type}_{sender_id}",
        }
    return _make


# ── New Sprint 1 fixtures ──────────────────────────────────────────


def pytest_configure(config):
    """Register custom markers to avoid PytestUnknownMarkWarning."""
    config.addinivalue_line("markers", "unit: fast unit tests (no network, no I/O)")
    config.addinivalue_line("markers", "integration: integration tests using LocalTransport")
    config.addinivalue_line("markers", "e2e: end-to-end tests requiring XMTP bridge")
    config.addinivalue_line("markers", "stress: stress/load tests")
    config.addinivalue_line("markers", "privacy: privacy and data leakage tests")


@pytest.fixture
def local_transport_pair():
    """Two agents sharing a LocalTransport message bus.

    Returns (transport, agent_a_name, agent_b_name) tuple.
    """
    from agentfax_collab import LocalTransport

    transport = LocalTransport()
    transport.register("agent-alpha")
    transport.register("agent-beta")
    return transport, "agent-alpha", "agent-beta"


@pytest.fixture
def alpha_agent(tmp_data_dir):
    """Fully initialized researcher-alpha LocalAgent."""
    from agentfax_collab import LocalTransport, LocalAgent, _load_researcher_skills

    transport = LocalTransport()
    agent = LocalAgent(
        "researcher-alpha", tmp_data_dir + "/alpha", transport, _load_researcher_skills
    )
    yield agent
    agent.close()


@pytest.fixture
def beta_agent(tmp_data_dir):
    """Fully initialized writer-beta LocalAgent."""
    from agentfax_collab import LocalTransport, LocalAgent, _load_writer_skills

    transport = LocalTransport()
    agent = LocalAgent(
        "writer-beta", tmp_data_dir + "/beta", transport, _load_writer_skills
    )
    yield agent
    agent.close()


@pytest.fixture
def make_agent(tmp_data_dir):
    """Factory fixture for creating LocalAgents with custom skills.

    Usage:
        agent = make_agent("my-agent", {"my_skill": lambda d: {"result": "ok"}})
    """
    from agentfax_collab import LocalTransport, LocalAgent

    agents = []
    _shared_transport = LocalTransport()

    def _factory(name, skills_dict=None):
        def load_skills(executor):
            if skills_dict:
                for skill_name, func in skills_dict.items():
                    executor.register_skill(
                        skill_name, func, description=f"Test skill: {skill_name}"
                    )

        _shared_transport.register(name)
        agent = LocalAgent(
            name, tmp_data_dir + f"/{name}", _shared_transport, load_skills
        )
        agents.append(agent)
        return agent

    yield _factory

    for agent in agents:
        try:
            agent.close()
        except Exception:
            pass
