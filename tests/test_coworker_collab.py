"""Comprehensive CoWorker collaboration tests — trust, privacy, skill visibility.

Tests the full collaboration flow with trust enforcement:
  1. UNTRUSTED peer sees zero skills on discover
  2. Trust request → grant/deny flow
  3. KNOWN peer sees filtered skills
  4. Skill-level trust enforcement on task_request
  5. Session trust tier tracking
  6. Collaboration with mixed trust levels
  7. Privacy: no data leaks in error responses
"""

import json
import os
import tempfile
import time
import threading
import pytest

# Add SDK to path
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk", "src"))

from coworker.agent import Agent, _uid, _now_iso
from coworker._internal.executor import TaskExecutor, SkillDefinition
from coworker._internal.security import TrustManager, TrustTier, MIN_TRUST_BY_MSG_TYPE, RESPONSE_MSG_TYPES


# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture
def data_dir(tmp_path):
    d = str(tmp_path / "coworker_test")
    os.makedirs(d, exist_ok=True)
    # Write minimal config
    with open(os.path.join(d, "config.json"), "w") as f:
        json.dump({"name": "test-agent", "wallet": "0xMY_WALLET"}, f)
    return d


@pytest.fixture
def executor_with_tiered_skills():
    """Executor with skills at different trust tiers."""
    e = TaskExecutor()

    @e.skill("public_ping", description="Public liveness", min_trust_tier=0)
    def public_ping(msg=""):
        return {"pong": True}

    @e.skill("search", description="Web search", min_trust_tier=1)
    def search(query=""):
        return {"results": [f"Result for: {query}"]}

    @e.skill("analyze", description="Deep analysis", min_trust_tier=1)
    def analyze(data=""):
        return {"analysis": f"Analyzed: {data}"}

    @e.skill("internal_debug", description="Internal diagnostics", min_trust_tier=2)
    def internal_debug(cmd=""):
        return {"debug": cmd}

    @e.skill("admin_reset", description="Admin reset", min_trust_tier=3)
    def admin_reset():
        return {"reset": True}

    return e


@pytest.fixture
def trust_mgr(data_dir):
    return TrustManager(data_dir)


@pytest.fixture
def trust_mgr_auto(data_dir):
    return TrustManager(data_dir, auto_accept_trust=True, max_auto_accept_tier=TrustTier.KNOWN)


# ── Test: Trust Tier System ────────────────────────────────


class TestTrustTiers:
    """Trust tier basics."""

    def test_default_untrusted(self, trust_mgr):
        assert trust_mgr.get_trust_tier("0xUNKNOWN") == TrustTier.UNTRUSTED

    def test_set_and_get_tier(self, trust_mgr):
        trust_mgr.set_trust_tier("0xPEER", TrustTier.KNOWN)
        assert trust_mgr.get_trust_tier("0xPEER") == TrustTier.KNOWN

    def test_persist_trust_override(self, data_dir):
        tm1 = TrustManager(data_dir)
        tm1.set_trust_override("0xAAA", TrustTier.INTERNAL)
        # New instance should load from disk
        tm2 = TrustManager(data_dir)
        assert tm2.get_trust_tier("0xAAA") == TrustTier.INTERNAL

    def test_remove_trust_override(self, trust_mgr):
        trust_mgr.set_trust_override("0xPEER", TrustTier.KNOWN)
        trust_mgr.remove_trust_override("0xPEER")
        assert trust_mgr.get_trust_tier("0xPEER") == TrustTier.UNTRUSTED

    def test_tier_ordering(self):
        assert TrustTier.UNTRUSTED < TrustTier.KNOWN
        assert TrustTier.KNOWN < TrustTier.INTERNAL
        assert TrustTier.INTERNAL < TrustTier.PRIVILEGED

    def test_all_tiers_property(self, trust_mgr):
        trust_mgr.set_trust_tier("0xA", TrustTier.KNOWN)
        trust_mgr.set_trust_tier("0xB", TrustTier.INTERNAL)
        tiers = trust_mgr.all_tiers
        assert tiers["0xA"] == "KNOWN"
        assert tiers["0xB"] == "INTERNAL"


# ── Test: Message ACL ─────────────────────────────────────


class TestMessageACL:
    """Message-level trust enforcement."""

    def test_untrusted_can_ping(self, trust_mgr):
        assert trust_mgr.is_message_allowed("0xSTRANGER", "ping")

    def test_untrusted_can_discover(self, trust_mgr):
        assert trust_mgr.is_message_allowed("0xSTRANGER", "discover")

    def test_untrusted_can_trust_request(self, trust_mgr):
        assert trust_mgr.is_message_allowed("0xSTRANGER", "trust_request")

    def test_untrusted_cannot_task_request(self, trust_mgr):
        assert not trust_mgr.is_message_allowed("0xSTRANGER", "task_request")

    def test_untrusted_cannot_plan_propose(self, trust_mgr):
        assert not trust_mgr.is_message_allowed("0xSTRANGER", "plan_propose")

    def test_untrusted_cannot_session_propose(self, trust_mgr):
        assert not trust_mgr.is_message_allowed("0xSTRANGER", "session_propose")

    def test_untrusted_cannot_context_query(self, trust_mgr):
        assert not trust_mgr.is_message_allowed("0xSTRANGER", "context_query")

    def test_known_can_task_request(self, trust_mgr):
        trust_mgr.set_trust_tier("0xPEER", TrustTier.KNOWN)
        assert trust_mgr.is_message_allowed("0xPEER", "task_request")

    def test_known_cannot_context_query(self, trust_mgr):
        trust_mgr.set_trust_tier("0xPEER", TrustTier.KNOWN)
        assert not trust_mgr.is_message_allowed("0xPEER", "context_query")

    def test_internal_can_context_query(self, trust_mgr):
        trust_mgr.set_trust_tier("0xPEER", TrustTier.INTERNAL)
        assert trust_mgr.is_message_allowed("0xPEER", "context_query")

    def test_unknown_msg_type_requires_privileged(self, trust_mgr):
        """Unknown message types default to PRIVILEGED (deny by default)."""
        trust_mgr.set_trust_tier("0xPEER", TrustTier.INTERNAL)
        assert not trust_mgr.is_message_allowed("0xPEER", "some_new_type")
        trust_mgr.set_trust_tier("0xPEER", TrustTier.PRIVILEGED)
        assert trust_mgr.is_message_allowed("0xPEER", "some_new_type")

    def test_rejection_info(self, trust_mgr):
        info = trust_mgr.get_rejection_info("0xSTRANGER", "task_request")
        assert info is not None
        assert info["code"] == "TRUST_TIER_TOO_LOW"
        assert info["peer_tier"] == 0
        assert info["required_tier"] == TrustTier.KNOWN

    def test_allowed_returns_no_rejection(self, trust_mgr):
        assert trust_mgr.get_rejection_info("0xSTRANGER", "ping") is None

    def test_response_types_at_untrusted(self, trust_mgr):
        """Response messages are UNTRUSTED (validated by correlation, not trust)."""
        for msg_type in RESPONSE_MSG_TYPES:
            assert trust_mgr.is_message_allowed("0xSTRANGER", msg_type), \
                f"Response type {msg_type} should be allowed for UNTRUSTED"


# ── Test: Skill Visibility ─────────────────────────────────


class TestSkillVisibility:
    """Skills visible only at sufficient trust tier."""

    def test_untrusted_sees_nothing_by_default(self, executor_with_tiered_skills):
        """Default min_trust_tier=1, so UNTRUSTED(0) sees no default skills."""
        e = executor_with_tiered_skills
        visible = e.list_skills_for_tier(0)
        # Only public_ping has min_trust_tier=0
        assert len(visible) == 1
        assert visible[0]["name"] == "public_ping"

    def test_known_sees_known_skills(self, executor_with_tiered_skills):
        e = executor_with_tiered_skills
        visible = e.list_skills_for_tier(1)
        names = {s["name"] for s in visible}
        assert "public_ping" in names
        assert "search" in names
        assert "analyze" in names
        assert "internal_debug" not in names
        assert "admin_reset" not in names

    def test_internal_sees_internal_skills(self, executor_with_tiered_skills):
        e = executor_with_tiered_skills
        visible = e.list_skills_for_tier(2)
        names = {s["name"] for s in visible}
        assert "internal_debug" in names
        assert "admin_reset" not in names

    def test_privileged_sees_all(self, executor_with_tiered_skills):
        e = executor_with_tiered_skills
        visible = e.list_skills_for_tier(3)
        assert len(visible) == 5  # all skills

    def test_list_skills_returns_all(self, executor_with_tiered_skills):
        """list_skills() (no tier) returns everything — for internal use only."""
        e = executor_with_tiered_skills
        assert len(e.list_skills()) == 5

    def test_filter_skills_for_peer(self, trust_mgr, executor_with_tiered_skills):
        """TrustManager.filter_skills_for_peer uses peer's actual trust tier."""
        e = executor_with_tiered_skills
        all_skills = e.list_skills()

        # UNTRUSTED peer
        visible = trust_mgr.filter_skills_for_peer("0xSTRANGER", all_skills)
        assert len(visible) == 1  # only public_ping

        # KNOWN peer
        trust_mgr.set_trust_tier("0xFRIEND", TrustTier.KNOWN)
        visible = trust_mgr.filter_skills_for_peer("0xFRIEND", all_skills)
        assert len(visible) == 3  # public_ping + search + analyze


# ── Test: Trust Request Flow ───────────────────────────────


class TestTrustRequest:
    """Trust request → grant/deny lifecycle."""

    def test_auto_accept_within_max(self, trust_mgr_auto):
        result = trust_mgr_auto.handle_trust_request("0xNEW", TrustTier.KNOWN, "want to collaborate")
        assert result["granted"] is True
        assert result["granted_tier"] == TrustTier.KNOWN
        assert trust_mgr_auto.get_trust_tier("0xNEW") == TrustTier.KNOWN

    def test_auto_deny_above_max(self, trust_mgr_auto):
        result = trust_mgr_auto.handle_trust_request("0xNEW", TrustTier.INTERNAL, "need deep access")
        assert result["granted"] is False

    def test_manual_mode_always_denies(self, trust_mgr):
        """Without auto_accept_trust, all requests need manual approval."""
        result = trust_mgr.handle_trust_request("0xNEW", TrustTier.KNOWN)
        assert result["granted"] is False
        assert "manual approval" in result["reason"]

    def test_trust_persists_after_grant(self, data_dir):
        tm = TrustManager(data_dir, auto_accept_trust=True)
        tm.handle_trust_request("0xGRANTED", TrustTier.KNOWN)
        # Reload from disk
        tm2 = TrustManager(data_dir)
        assert tm2.get_trust_tier("0xGRANTED") == TrustTier.KNOWN


# ── Test: Agent Trust Integration ──────────────────────────


class TestAgentTrustIntegration:
    """Agent._handle_message trust enforcement."""

    def _make_agent(self, data_dir, auto_accept=False):
        agent = Agent("test-bot", data_dir=data_dir, auto_accept_trust=auto_accept)
        # Register tiered skills
        @agent.skill("public_api", description="Public API", min_trust_tier=0)
        def public_api(msg=""):
            return {"ok": True}

        @agent.skill("search", description="Search", min_trust_tier=1)
        def search(query=""):
            return {"results": [query]}

        @agent.skill("debug", description="Debug", min_trust_tier=2)
        def debug(cmd=""):
            return {"debug": cmd}

        return agent

    def _make_msg(self, msg_type, sender="0xSTRANGER", payload=None, corr_id=None):
        return {
            "type": msg_type,
            "sender_wallet": sender,
            "sender_id": "stranger",
            "correlation_id": corr_id or _uid(),
            "payload": payload or {},
        }

    def test_agent_has_trust_manager(self, data_dir):
        agent = self._make_agent(data_dir)
        assert agent.trust_manager is not None
        assert isinstance(agent.trust_manager, TrustManager)

    def test_discover_returns_zero_skills_for_untrusted(self, data_dir):
        """UNTRUSTED peer discovers sees only min_trust_tier=0 skills."""
        agent = self._make_agent(data_dir)
        # Mock client.send to capture what was sent
        sent = []
        agent._client = type("MockClient", (), {
            "send": lambda self, wallet, msg_type, payload, correlation_id="": sent.append(
                {"wallet": wallet, "type": msg_type, "payload": payload}
            ),
        })()

        agent._handle_message(self._make_msg("discover", "0xSTRANGER"))

        assert len(sent) == 1
        assert sent[0]["type"] == "capabilities"
        skills = sent[0]["payload"]["skills"]
        names = [s["name"] for s in skills]
        assert "public_api" in names
        assert "search" not in names  # min_trust_tier=1, peer is UNTRUSTED(0)
        assert "debug" not in names

    def test_discover_returns_known_skills_for_known_peer(self, data_dir):
        agent = self._make_agent(data_dir)
        agent.trust_manager.set_trust_tier("0xFRIEND", TrustTier.KNOWN)

        sent = []
        agent._client = type("MockClient", (), {
            "send": lambda self, wallet, msg_type, payload, correlation_id="": sent.append(
                {"wallet": wallet, "type": msg_type, "payload": payload}
            ),
        })()

        agent._handle_message(self._make_msg("discover", "0xFRIEND"))

        skills = sent[0]["payload"]["skills"]
        names = [s["name"] for s in skills]
        assert "public_api" in names
        assert "search" in names
        assert "debug" not in names  # needs INTERNAL

    def test_task_request_blocked_for_untrusted(self, data_dir):
        """UNTRUSTED peer cannot send task_request."""
        agent = self._make_agent(data_dir)
        sent = []
        agent._client = type("MockClient", (), {
            "send": lambda self, wallet, msg_type, payload, correlation_id="": sent.append(
                {"wallet": wallet, "type": msg_type, "payload": payload}
            ),
        })()

        agent._handle_message(self._make_msg(
            "task_request", "0xSTRANGER",
            payload={"skill": "search", "input": {"query": "test"}}
        ))

        # Should get error, not task_response
        assert len(sent) == 1
        assert sent[0]["type"] == "error"
        assert "TRUST_TIER_TOO_LOW" in str(sent[0]["payload"])

    def test_task_request_allowed_for_known(self, data_dir):
        agent = self._make_agent(data_dir)
        agent.trust_manager.set_trust_tier("0xFRIEND", TrustTier.KNOWN)

        sent = []
        agent._client = type("MockClient", (), {
            "send": lambda self, wallet, msg_type, payload, correlation_id="": sent.append(
                {"wallet": wallet, "type": msg_type, "payload": payload}
            ),
        })()

        agent._handle_message(self._make_msg(
            "task_request", "0xFRIEND",
            payload={"skill": "search", "input": {"query": "hello"}}
        ))

        assert len(sent) == 1
        assert sent[0]["type"] == "task_response"
        assert sent[0]["payload"]["success"] is True

    def test_skill_level_trust_enforcement(self, data_dir):
        """KNOWN peer cannot call INTERNAL skill."""
        agent = self._make_agent(data_dir)
        agent.trust_manager.set_trust_tier("0xFRIEND", TrustTier.KNOWN)

        sent = []
        agent._client = type("MockClient", (), {
            "send": lambda self, wallet, msg_type, payload, correlation_id="": sent.append(
                {"wallet": wallet, "type": msg_type, "payload": payload}
            ),
        })()

        agent._handle_message(self._make_msg(
            "task_request", "0xFRIEND",
            payload={"skill": "debug", "input": {"cmd": "status"}}
        ))

        assert sent[0]["type"] == "task_error"
        assert "trust tier" in sent[0]["payload"]["error"].lower()

    def test_response_messages_bypass_trust_gate(self, data_dir):
        """Response messages go to response_box regardless of trust tier."""
        agent = self._make_agent(data_dir)
        corr = "test_corr_123"
        msg = self._make_msg("task_response", "0xSTRANGER", {"success": True}, corr_id=corr)
        agent._handle_message(msg)
        assert corr in agent._response_box

    def test_trust_request_handler(self, data_dir):
        """Trust request flow via agent._handle_message."""
        agent = self._make_agent(data_dir, auto_accept=True)

        sent = []
        agent._client = type("MockClient", (), {
            "send": lambda self, wallet, msg_type, payload, correlation_id="": sent.append(
                {"wallet": wallet, "type": msg_type, "payload": payload}
            ),
        })()

        agent._handle_message(self._make_msg(
            "trust_request", "0xNEWBIE",
            payload={"requested_tier": 1, "reason": "want to search"}
        ))

        assert len(sent) == 1
        assert sent[0]["type"] == "trust_grant"
        assert sent[0]["payload"]["granted"] is True

        # Now the peer should be KNOWN
        assert agent.trust_manager.get_trust_tier("0xNEWBIE") == TrustTier.KNOWN


# ── Test: Full Collaboration Flow ──────────────────────────


class TestCollaborationWithTrust:
    """End-to-end collaboration scenarios with trust enforcement."""

    def test_first_contact_flow(self, data_dir):
        """Simulate: stranger → discover (sees nothing) → trust_request → granted → discover (sees skills)."""
        agent = Agent("collab-bot", data_dir=data_dir, auto_accept_trust=True)

        @agent.skill("translate", description="Translate text")
        def translate(text="", to_lang="en"):
            return {"translated": f"[{to_lang}] {text}"}

        sent = []
        agent._client = type("MockClient", (), {
            "send": lambda self, wallet, msg_type, payload, correlation_id="": sent.append(
                {"wallet": wallet, "type": msg_type, "payload": payload}
            ),
        })()

        stranger = "0xFIRST_CONTACT"

        # Step 1: Discover — sees nothing (default min_trust_tier=1, peer is 0)
        agent._handle_message({
            "type": "discover", "sender_wallet": stranger,
            "correlation_id": "d1", "payload": {},
        })
        skills_before = sent[-1]["payload"]["skills"]
        assert len(skills_before) == 0

        # Step 2: Trust request
        agent._handle_message({
            "type": "trust_request", "sender_wallet": stranger,
            "correlation_id": "t1", "payload": {"requested_tier": 1, "reason": "want to translate"},
        })
        assert sent[-1]["type"] == "trust_grant"

        # Step 3: Discover again — now sees translate
        agent._handle_message({
            "type": "discover", "sender_wallet": stranger,
            "correlation_id": "d2", "payload": {},
        })
        skills_after = sent[-1]["payload"]["skills"]
        assert len(skills_after) == 1
        assert skills_after[0]["name"] == "translate"

        # Step 4: Call translate
        agent._handle_message({
            "type": "task_request", "sender_wallet": stranger,
            "correlation_id": "c1", "payload": {"skill": "translate", "input": {"text": "hello", "to_lang": "zh"}},
        })
        assert sent[-1]["type"] == "task_response"
        assert sent[-1]["payload"]["success"] is True

    def test_privacy_no_stack_trace_in_errors(self, data_dir):
        """Error responses should not leak internal implementation details."""
        agent = Agent("safe-bot", data_dir=data_dir)

        sent = []
        agent._client = type("MockClient", (), {
            "send": lambda self, wallet, msg_type, payload, correlation_id="": sent.append(
                {"wallet": wallet, "type": msg_type, "payload": payload}
            ),
        })()

        # Untrusted peer tries task_request
        agent._handle_message({
            "type": "task_request", "sender_wallet": "0xATTACKER",
            "correlation_id": "x1", "payload": {"skill": "anything"},
        })

        error_payload = json.dumps(sent[-1]["payload"])
        assert "Traceback" not in error_payload
        assert "File " not in error_payload
        assert "/Users/" not in error_payload

    def test_plan_propose_blocked_for_untrusted(self, data_dir):
        """UNTRUSTED peer cannot propose a plan."""
        agent = Agent("safe-bot", data_dir=data_dir)

        sent = []
        agent._client = type("MockClient", (), {
            "send": lambda self, wallet, msg_type, payload, correlation_id="": sent.append(
                {"wallet": wallet, "type": msg_type, "payload": payload}
            ),
        })()

        agent._handle_message({
            "type": "plan_propose", "sender_wallet": "0xUNTRUSTED",
            "correlation_id": "p1",
            "payload": {"goal": "steal data", "steps": []},
        })

        assert sent[-1]["type"] == "error"


# ── Test: Executor Edge Cases ──────────────────────────────


class TestExecutorEdgeCases:
    """Edge cases in skill execution with trust tiers."""

    def test_min_trust_tier_zero_visible_to_all(self):
        e = TaskExecutor()

        @e.skill("open_api", min_trust_tier=0)
        def open_api():
            return {"open": True}

        assert len(e.list_skills_for_tier(0)) == 1

    def test_default_min_trust_tier_is_known(self):
        e = TaskExecutor()

        @e.skill("default_skill")
        def default_skill():
            return {}

        # Default min_trust_tier=1 (KNOWN)
        assert len(e.list_skills_for_tier(0)) == 0
        assert len(e.list_skills_for_tier(1)) == 1

    def test_skill_to_dict_includes_trust_fields(self):
        e = TaskExecutor()

        @e.skill("my_skill", min_trust_tier=2, max_context_privacy_tier="L2_TRUSTED")
        def my_skill():
            return {}

        d = e.list_skills()[0]
        assert d["min_trust_tier"] == 2
        assert d["max_context_privacy_tier"] == "L2_TRUSTED"

    def test_execute_unknown_skill(self):
        e = TaskExecutor()
        result = e.execute("nonexistent", {})
        assert result["success"] is False
        assert "Unknown skill" in result["error"]


# ── Test: TrustManager Persistence ─────────────────────────


class TestTrustPersistence:
    """Trust data persists correctly across restarts."""

    def test_save_and_load_multiple_peers(self):
        with tempfile.TemporaryDirectory() as td:
            tm = TrustManager(td)
            tm.set_trust_override("0xA", TrustTier.KNOWN)
            tm.set_trust_override("0xB", TrustTier.INTERNAL)
            tm.set_trust_override("0xC", TrustTier.PRIVILEGED)

            tm2 = TrustManager(td)
            assert tm2.get_trust_tier("0xA") == TrustTier.KNOWN
            assert tm2.get_trust_tier("0xB") == TrustTier.INTERNAL
            assert tm2.get_trust_tier("0xC") == TrustTier.PRIVILEGED

    def test_corrupt_trust_file_graceful(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "trust.json"), "w") as f:
                f.write("not valid json{{{")
            tm = TrustManager(td)
            # Should not crash, defaults to empty
            assert tm.get_trust_tier("0xANY") == TrustTier.UNTRUSTED

    def test_trust_file_format(self):
        with tempfile.TemporaryDirectory() as td:
            tm = TrustManager(td)
            tm.set_trust_override("0xA", TrustTier.KNOWN)

            with open(os.path.join(td, "trust.json")) as f:
                data = json.load(f)
            assert data["0xA"] == "known"  # stored as lowercase name
