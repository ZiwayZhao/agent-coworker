#!/usr/bin/env python3
"""Tests for S1: Skill Card data model, registry, peer cache, handler integration."""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "agent-fax", "scripts"))

from executor import TaskExecutor, SkillDefinition, register_builtin_skills
from skill_registry import SkillCard, PeerSkillCache
from peers import PeerManager


# ── SkillCard data class ─────────────────────────────────────────

class TestSkillCard(unittest.TestCase):
    """Test SkillCard creation, serialization, deserialization."""

    def test_basic_creation(self):
        card = SkillCard(skill_name="echo", description="Echo input back")
        self.assertEqual(card.skill_name, "echo")
        self.assertEqual(card.description, "Echo input back")
        self.assertEqual(card.skill_version, "1.0.0")
        self.assertEqual(card.min_trust_tier, 1)
        self.assertEqual(card.max_context_privacy_tier, "L1_PUBLIC")

    def test_full_creation(self):
        card = SkillCard(
            skill_name="analysis",
            description="Analyze data",
            skill_version="2.1.0",
            provider_agent_id="ziway",
            provider_wallet="0xabc",
            input_schema={"type": "object", "properties": {"data": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"result": {"type": "string"}}},
            min_trust_tier=2,
            max_context_privacy_tier="L2_TRUSTED",
            pricing_model="per_call",
            pricing_amount=0.01,
            task_ttl_seconds=60,
            idempotent=True,
            tags=["data", "analysis"],
            examples=[{"input": {"data": "x"}, "output": {"result": "y"}}],
        )
        self.assertEqual(card.min_trust_tier, 2)
        self.assertEqual(card.pricing_model, "per_call")
        self.assertTrue(card.idempotent)
        self.assertEqual(card.tags, ["data", "analysis"])

    def test_to_dict_schema(self):
        """Verify to_dict() matches the canonical Skill Card JSON schema."""
        card = SkillCard(
            skill_name="test",
            provider_agent_id="z",
            provider_wallet="0x1",
            tags=["demo"],
        )
        d = card.to_dict()

        # Top-level required keys
        self.assertEqual(d["schema_version"], "1.0")
        self.assertEqual(d["skill_name"], "test")
        self.assertIn("provider", d)
        self.assertIn("trust_requirements", d)
        self.assertIn("pricing", d)
        self.assertIn("timeouts", d)
        self.assertIn("capabilities", d)
        self.assertIn("schema_hash", d)
        self.assertIn("updated_at", d)

        # Nested structure
        self.assertEqual(d["provider"]["agent_id"], "z")
        self.assertEqual(d["provider"]["wallet"], "0x1")
        self.assertEqual(d["trust_requirements"]["min_trust_tier"], 1)
        self.assertEqual(d["pricing"]["model"], "free")
        self.assertEqual(d["tags"], ["demo"])

    def test_schema_hash_deterministic(self):
        card1 = SkillCard(
            skill_name="a",
            input_schema={"x": "int"},
            output_schema={"y": "str"},
        )
        card2 = SkillCard(
            skill_name="b",
            input_schema={"x": "int"},
            output_schema={"y": "str"},
        )
        # Same schemas → same hash
        self.assertEqual(card1.schema_hash, card2.schema_hash)

    def test_schema_hash_changes_with_schema(self):
        card1 = SkillCard(skill_name="a", input_schema={"x": "int"})
        card2 = SkillCard(skill_name="a", input_schema={"x": "str"})
        self.assertNotEqual(card1.schema_hash, card2.schema_hash)

    def test_from_dict_roundtrip(self):
        original = SkillCard(
            skill_name="rt",
            description="Roundtrip test",
            skill_version="1.2.3",
            provider_agent_id="agent",
            min_trust_tier=2,
            pricing_model="per_call",
            pricing_amount=0.5,
            idempotent=True,
            tags=["test"],
        )
        d = original.to_dict()
        restored = SkillCard.from_dict(d)

        self.assertEqual(restored.skill_name, "rt")
        self.assertEqual(restored.skill_version, "1.2.3")
        self.assertEqual(restored.min_trust_tier, 2)
        self.assertEqual(restored.pricing_model, "per_call")
        self.assertEqual(restored.pricing_amount, 0.5)
        self.assertTrue(restored.idempotent)
        self.assertEqual(restored.tags, ["test"])

    def test_from_skill_def(self):
        sd = SkillDefinition("echo", lambda x: x, description="Echo",
                             min_trust_tier=2)
        card = SkillCard.from_skill_def(sd, agent_id="z", wallet="0x1")
        self.assertEqual(card.skill_name, "echo")
        self.assertEqual(card.min_trust_tier, 2)
        self.assertEqual(card.provider_agent_id, "z")


# ── SkillDefinition.to_skill_card() ────────────────────────────

class TestSkillDefToCard(unittest.TestCase):
    """Test executor's to_skill_card() bridge."""

    def test_to_skill_card_returns_skill_card(self):
        sd = SkillDefinition("reverse", lambda x: x, description="Reverse text",
                             input_schema={"text": "string"})
        card = sd.to_skill_card(agent_id="z", wallet="0x1")
        self.assertIsInstance(card, SkillCard)
        self.assertEqual(card.skill_name, "reverse")
        self.assertEqual(card.input_schema, {"text": "string"})

    def test_list_skill_cards(self):
        ex = TaskExecutor()
        register_builtin_skills(ex)
        cards = ex.list_skill_cards(agent_id="test", wallet="0xtest")
        self.assertGreater(len(cards), 0)
        for card in cards:
            self.assertIn("skill_name", card)
            self.assertIn("schema_version", card)
            self.assertIn("provider", card)
            self.assertEqual(card["provider"]["agent_id"], "test")


# ── PeerSkillCache (SQLite) ────────────────────────────────────

class TestPeerSkillCache(unittest.TestCase):
    """Test SQLite-backed peer skill card cache."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache = PeerSkillCache(self.tmpdir)

    def tearDown(self):
        self.cache.close()

    def _make_card(self, name="echo", peer="icy", tags=None):
        return {
            "skill_name": name,
            "skill_version": "1.0.0",
            "description": f"Skill: {name}",
            "trust_requirements": {"min_trust_tier": 1},
            "tags": tags or [],
        }

    def test_store_and_retrieve(self):
        cards = [self._make_card("echo"), self._make_card("reverse")]
        stored = self.cache.store_cards("icy", cards)
        self.assertEqual(stored, 2)

        retrieved = self.cache.get_cards("icy")
        self.assertEqual(len(retrieved), 2)
        names = {c["skill_name"] for c in retrieved}
        self.assertEqual(names, {"echo", "reverse"})

    def test_upsert_on_duplicate(self):
        card1 = self._make_card("echo")
        card1["description"] = "v1"
        self.cache.store_cards("icy", [card1])

        card2 = self._make_card("echo")
        card2["description"] = "v2"
        self.cache.store_cards("icy", [card2])

        cards = self.cache.get_cards("icy")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["description"], "v2")

    def test_get_card_single(self):
        self.cache.store_cards("icy", [self._make_card("echo")])
        card = self.cache.get_card("icy", "echo")
        self.assertIsNotNone(card)
        self.assertEqual(card["skill_name"], "echo")

        missing = self.cache.get_card("icy", "nonexistent")
        self.assertIsNone(missing)

    def test_find_by_skill(self):
        self.cache.store_cards("icy", [self._make_card("echo")])
        self.cache.store_cards("bot2", [self._make_card("echo")])
        self.cache.store_cards("bot3", [self._make_card("reverse")])

        results = self.cache.find_by_skill("echo")
        self.assertEqual(len(results), 2)
        peers = {r["peer_id"] for r in results}
        self.assertEqual(peers, {"icy", "bot2"})

    def test_find_by_tag(self):
        self.cache.store_cards("icy", [self._make_card("echo", tags=["demo", "basic"])])
        self.cache.store_cards("bot2", [self._make_card("analyze", tags=["data"])])

        results = self.cache.find_by_tag("demo")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["peer_id"], "icy")

    def test_count(self):
        self.cache.store_cards("icy", [self._make_card("a"), self._make_card("b")])
        self.cache.store_cards("bot2", [self._make_card("c")])

        self.assertEqual(self.cache.count(), 3)
        self.assertEqual(self.cache.count("icy"), 2)
        self.assertEqual(self.cache.count("bot2"), 1)

    def test_list_all_peers(self):
        self.cache.store_cards("icy", [self._make_card()])
        self.cache.store_cards("bot2", [self._make_card()])
        peers = self.cache.list_all_peers()
        self.assertEqual(set(peers), {"icy", "bot2"})

    def test_clear_peer(self):
        self.cache.store_cards("icy", [self._make_card()])
        self.cache.clear_peer("icy")
        self.assertEqual(self.cache.count("icy"), 0)

    def test_expired_cards_not_returned(self):
        """Cards with past expiry should not be returned by default."""
        self.cache.store_cards("icy", [self._make_card()], ttl_seconds=-1)
        cards = self.cache.get_cards("icy")
        self.assertEqual(len(cards), 0)

        # But with include_expired=True
        cards_all = self.cache.get_cards("icy", include_expired=True)
        self.assertEqual(len(cards_all), 1)

    def test_evict_expired(self):
        self.cache.store_cards("icy", [self._make_card()], ttl_seconds=-1)
        evicted = self.cache.evict_expired()
        self.assertEqual(evicted, 1)
        self.assertEqual(self.cache.count(), 0)

    def test_backwards_compat_name_field(self):
        """Cards using old 'name' field instead of 'skill_name' still work."""
        old_format = {"name": "legacy_skill", "description": "old format"}
        stored = self.cache.store_cards("old_peer", [old_format])
        self.assertEqual(stored, 1)
        cards = self.cache.get_cards("old_peer")
        self.assertEqual(len(cards), 1)


# ── PeerManager + SkillCache integration ─────────────────────

class TestPeerManagerSkillCache(unittest.TestCase):
    """Test PeerManager integration with PeerSkillCache."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.peer_mgr = PeerManager(self.tmpdir)
        self.cache = PeerSkillCache(self.tmpdir)
        self.peer_mgr.set_skill_cache(self.cache)

    def tearDown(self):
        self.cache.close()

    def test_get_skill_cards(self):
        self.cache.store_cards("icy", [
            {"skill_name": "echo", "description": "Echo"},
        ])
        cards = self.peer_mgr.get_skill_cards("icy")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["skill_name"], "echo")

    def test_find_by_skill_card(self):
        self.peer_mgr.update_seen("icy", wallet="0x1")
        self.cache.store_cards("icy", [
            {"skill_name": "analyze", "description": "Analyze data"},
        ])
        results = self.peer_mgr.find_by_skill_card("analyze")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "icy")
        self.assertIn("card", results[0])

    def test_find_by_skill_card_no_cache(self):
        """Without skill cache, falls back to basic find_by_skill."""
        mgr = PeerManager(self.tmpdir)
        # No set_skill_cache
        mgr.update_capabilities("icy", capabilities={
            "skills": [{"name": "echo"}]
        })
        results = mgr.find_by_skill_card("echo")
        self.assertEqual(len(results), 1)


# ── Skill Handler integration ───────────────────────────────────

class TestSkillHandlerS1(unittest.TestCase):
    """Test enhanced skill_handler with full Skill Cards."""

    def setUp(self):
        from router import MessageRouter, RouterContext
        self.tmpdir = tempfile.mkdtemp()
        self.router = MessageRouter()
        self.executor = TaskExecutor()
        register_builtin_skills(self.executor)
        self.cache = PeerSkillCache(self.tmpdir)

        self.ctx = RouterContext(client=MagicMock())
        # Mock client attributes for identity
        self.ctx.client.agent_id = "ziway"
        self.ctx.client.wallet_address = "0xtest"

        from handlers.skill_handler import register_skill_handlers
        register_skill_handlers(
            self.router, self.executor, self.tmpdir,
            peer_skill_cache=self.cache,
        )

    def tearDown(self):
        self.cache.close()

    def test_skill_card_query_returns_full_cards(self):
        msg = {
            "type": "skill_card_query",
            "sender_id": "icy",
            "payload": {},
        }
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "skill_card_list")

        cards = result["payload"]["skills"]
        self.assertGreater(len(cards), 0)

        # Verify full Skill Card schema
        card = cards[0]
        self.assertIn("schema_version", card)
        self.assertIn("skill_name", card)
        self.assertIn("provider", card)
        self.assertIn("trust_requirements", card)
        self.assertIn("pricing", card)
        self.assertIn("timeouts", card)
        self.assertIn("capabilities", card)
        self.assertIn("schema_hash", card)

        # Provider should be populated
        self.assertEqual(card["provider"]["agent_id"], "ziway")

    def test_skill_card_query_filter_by_names(self):
        msg = {
            "type": "skill_card_query",
            "sender_id": "icy",
            "payload": {"names": ["echo"]},
        }
        result = self.router.dispatch(msg, self.ctx)
        cards = result["payload"]["skills"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["skill_name"], "echo")

    def test_skill_card_query_filter_by_tags(self):
        # Register a tagged skill
        self.executor.register_skill(
            "tagged_skill", lambda x: x,
            description="A tagged skill",
        )
        # Built-in skills have no tags, so filtering by tag should return 0
        msg = {
            "type": "skill_card_query",
            "sender_id": "icy",
            "payload": {"tags": ["nonexistent_tag"]},
        }
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["payload"]["count"], 0)

    def test_skill_card_get_returns_full_card(self):
        msg = {
            "type": "skill_card_get",
            "sender_id": "icy",
            "payload": {"skill_name": "echo"},
        }
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "skill_card")
        card = result["payload"]["card"]
        self.assertEqual(card["skill_name"], "echo")
        self.assertIn("schema_version", card)
        self.assertEqual(card["provider"]["agent_id"], "ziway")

    def test_skill_card_list_caches_in_peer_skill_cache(self):
        """Incoming skill_card_list should be cached in PeerSkillCache."""
        peer_cards = [
            {"skill_name": "analyze", "skill_version": "1.0.0",
             "trust_requirements": {"min_trust_tier": 1}, "tags": []},
            {"skill_name": "summarize", "skill_version": "1.0.0",
             "trust_requirements": {"min_trust_tier": 2}, "tags": ["nlp"]},
        ]
        msg = {
            "type": "skill_card_list",
            "sender_id": "icy",
            "payload": {"skills": peer_cards, "count": 2},
        }
        self.router.dispatch(msg, self.ctx)

        # Verify cached
        cached = self.cache.get_cards("icy")
        self.assertEqual(len(cached), 2)
        names = {c["skill_name"] for c in cached}
        self.assertEqual(names, {"analyze", "summarize"})

    def test_skill_card_response_caches_single_card(self):
        """Incoming skill_card (single) should be cached."""
        card = {
            "skill_name": "special",
            "skill_version": "2.0.0",
            "description": "A special skill",
            "tags": ["special"],
        }
        msg = {
            "type": "skill_card",
            "sender_id": "icy",
            "payload": {"card": card},
        }
        self.router.dispatch(msg, self.ctx)

        cached = self.cache.get_card("icy", "special")
        self.assertIsNotNone(cached)
        self.assertEqual(cached["skill_version"], "2.0.0")

    def test_skill_card_list_updates_peer_skills_names(self):
        """Regression: skill_card_list with full cards must populate peer.skills correctly."""
        peer_mgr = MagicMock()
        self.ctx.peer_manager = peer_mgr

        peer_cards = [
            {"skill_name": "analyze", "skill_version": "1.0.0",
             "trust_requirements": {"min_trust_tier": 1}, "tags": []},
            {"skill_name": "summarize", "skill_version": "1.0.0",
             "trust_requirements": {"min_trust_tier": 2}, "tags": ["nlp"]},
        ]
        msg = {
            "type": "skill_card_list",
            "sender_id": "icy",
            "payload": {"skills": peer_cards, "count": 2},
        }
        self.router.dispatch(msg, self.ctx)

        # Verify update_capabilities was called with skills as objects
        peer_mgr.update_capabilities.assert_called_once()
        call_kwargs = peer_mgr.update_capabilities.call_args
        caps = call_kwargs[1].get("capabilities") or call_kwargs[0][1] if len(call_kwargs[0]) > 1 else call_kwargs[1]["capabilities"]
        skill_objs = caps["skills"]
        # Each skill object should have skill_name
        self.assertTrue(all(s.get("skill_name") for s in skill_objs))

    def test_peers_py_extracts_skill_name_from_full_cards(self):
        """Regression: PeerManager.update_capabilities() handles skill_name field."""
        peer_mgr = PeerManager(self.tmpdir)
        peer_mgr.update_capabilities("icy", capabilities={
            "skills": [
                {"skill_name": "echo", "skill_version": "1.0.0"},
                {"skill_name": "reverse", "skill_version": "1.0.0"},
            ]
        })
        peer = peer_mgr.get("icy")
        self.assertEqual(set(peer["skills"]), {"echo", "reverse"})

    def test_legacy_skill_query_still_works(self):
        msg = {"type": "skill_query", "sender_id": "old", "payload": {}}
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "skill_list")
        self.assertGreater(result["payload"]["count"], 0)

    def test_skill_install_still_rejected(self):
        msg = {
            "type": "skill_install",
            "sender_id": "hacker",
            "payload": {"name": "evil", "code": "import os"},
        }
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["payload"]["error_code"], "CODE_TRANSFER_FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
