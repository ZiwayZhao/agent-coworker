"""SkillManifest — structured skill metadata for CoWorker Protocol.

Supports both native @agent.skill() and wrapped SKILL.md skills.
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional, Dict, List


@dataclass
class SkillManifest:
    """Standard skill metadata shared during discover/AgentCard exchange."""
    name: str
    description: str = ""
    when_to_use: str = ""          # LLM routing hint
    version: str = ""              # semver
    category: str = "query"        # query/compute/action/orchestration/meta
    input_schema: Dict = field(default_factory=dict)
    output_schema: Dict = field(default_factory=dict)
    min_trust_tier: int = 1
    visibility: str = "public"     # public/restricted/hidden
    context: str = "inline"        # inline (sync) / fork (async isolated)
    # Origin tracking
    origin_type: str = "native"    # native / wrapped_skill_md / wrapped_mcp
    wrapped_from: str = ""         # original path/source if wrapped

    def schema_hash(self) -> str:
        """Deterministic hash of the public schema for cache invalidation."""
        payload = json.dumps({
            "name": self.name,
            "version": self.version,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "min_trust_tier": self.min_trust_tier,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def to_public_dict(self) -> dict:
        """Safe attributes only — shared during discover."""
        d = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "min_trust_tier": self.min_trust_tier,
            "category": self.category,
            "schema_hash": self.schema_hash(),
        }
        if self.when_to_use:
            d["when_to_use"] = self.when_to_use
        if self.version:
            d["version"] = self.version
        return d

    def to_summary(self) -> dict:
        """Minimal summary for AgentCard skill list."""
        d = {"name": self.name, "schema_hash": self.schema_hash()}
        if self.version:
            d["version"] = self.version
        if self.when_to_use:
            d["when_to_use"] = self.when_to_use
        return d


@dataclass
class AgentCard:
    """Identity + capabilities card exchanged during handshake."""
    agent_id: str
    display_name: str = ""
    wallet: str = ""
    inbox_id: str = ""
    skills: List[dict] = field(default_factory=list)  # list of summary dicts
    trust_tier: int = 0
    heartbeat_interval: int = 30
    protocol_version: str = "0.6.0"

    def schema_hash(self) -> str:
        """Hash of all skill schemas for cache invalidation."""
        payload = json.dumps(self.skills, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "display_name": self.display_name or self.agent_id,
            "wallet": self.wallet,
            "inbox_id": self.inbox_id,
            "skills": self.skills,
            "trust_tier": self.trust_tier,
            "heartbeat_interval": self.heartbeat_interval,
            "protocol_version": self.protocol_version,
            "card_hash": self.schema_hash(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentCard":
        return cls(
            agent_id=data.get("agent_id", ""),
            display_name=data.get("display_name", ""),
            wallet=data.get("wallet", ""),
            inbox_id=data.get("inbox_id", ""),
            skills=data.get("skills", []),
            trust_tier=data.get("trust_tier", 0),
            heartbeat_interval=data.get("heartbeat_interval", 30),
            protocol_version=data.get("protocol_version", "0.6.0"),
        )
