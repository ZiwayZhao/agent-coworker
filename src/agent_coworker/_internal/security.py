"""CoWorker Trust & Privacy — message ACL, trust tiers, skill visibility filtering.

Privacy model:
  - Default-deny: unknown wallets see NOTHING
  - Skills are only visible to peers whose trust tier >= skill.min_trust_tier
  - Message types require minimum trust tiers to process
  - Response messages are validated by correlation ID, not trust alone
"""

import enum
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Any, Optional

logger = logging.getLogger("coworker.security")


class TrustTier(enum.IntEnum):
    """Trust levels for peers — local perspective only."""
    UNTRUSTED = 0
    KNOWN = 1
    INTERNAL = 2
    PRIVILEGED = 3


# ── Message ACL ──────────────────────────────────────────
# Maps message type → minimum trust tier required to SEND this message.
# Unknown message types default to PRIVILEGED (deny by default).

MIN_TRUST_BY_MSG_TYPE: Dict[str, int] = {
    # Anyone can knock
    "ping":           TrustTier.UNTRUSTED,
    "discover":       TrustTier.UNTRUSTED,   # allowed, but sees filtered skills
    "trust_request":  TrustTier.UNTRUSTED,   # request to be trusted

    # Correlated responses (validated by response_box, not just trust)
    "pong":           TrustTier.UNTRUSTED,
    "capabilities":   TrustTier.UNTRUSTED,
    "trust_grant":    TrustTier.UNTRUSTED,
    "trust_deny":     TrustTier.UNTRUSTED,
    "task_response":  TrustTier.UNTRUSTED,
    "task_error":     TrustTier.UNTRUSTED,
    "plan_accept":    TrustTier.UNTRUSTED,
    "plan_reject":    TrustTier.UNTRUSTED,
    "session_accept": TrustTier.UNTRUSTED,
    "error":          TrustTier.UNTRUSTED,   # error responses from trust gate
    "skill_card":     TrustTier.UNTRUSTED,   # skill card responses

    # Require KNOWN+
    "task_request":    TrustTier.KNOWN,
    "session_propose": TrustTier.KNOWN,
    "plan_propose":    TrustTier.KNOWN,
    "skill_card_query": TrustTier.KNOWN,

    # Group messages — KNOWN+ can participate in groups
    "group_create":    TrustTier.KNOWN,
    "group_invite":    TrustTier.KNOWN,
    "group_message":   TrustTier.KNOWN,
    "group_discover":  TrustTier.KNOWN,
    "group_task_request": TrustTier.KNOWN,

    # Group responses (correlated)
    "group_created":     TrustTier.UNTRUSTED,
    "group_joined":      TrustTier.UNTRUSTED,
    "group_capabilities": TrustTier.UNTRUSTED,
    "group_task_response": TrustTier.UNTRUSTED,
    "group_task_error":  TrustTier.UNTRUSTED,

    # Require INTERNAL+
    "context_query":   TrustTier.INTERNAL,
}

# Group response message types (validated by correlation)
GROUP_RESPONSE_MSG_TYPES = frozenset({
    "group_created", "group_joined", "group_capabilities",
    "group_task_response", "group_task_error",
})

# Message types that are responses to requests (validated by correlation)
RESPONSE_MSG_TYPES = frozenset({
    "pong", "capabilities", "trust_grant", "trust_deny",
    "task_response", "task_error", "plan_accept", "plan_reject",
    "session_accept", "error", "skill_card",
    "group_created", "group_joined", "group_capabilities",
    "group_task_response", "group_task_error",
})


class TrustManager:
    """Manages local trust decisions about peers.

    Privacy-first design:
    - All peers start as UNTRUSTED
    - UNTRUSTED peers can ping and discover, but see NO skills by default
    - Skills are only visible when peer_tier >= skill.min_trust_tier
    - Trust must be explicitly granted (no auto-promotion)
    """

    def __init__(self, data_dir: str, auto_accept_trust: bool = True,
                 max_auto_accept_tier: int = TrustTier.KNOWN):
        self.data_dir = str(Path(data_dir).expanduser())
        self._trust_overrides: Dict[str, TrustTier] = {}
        self.auto_accept_trust = auto_accept_trust
        self.max_auto_accept_tier = max_auto_accept_tier
        self._trust_file_mtime: float = 0
        self._load_trust_overrides()

    def _load_trust_overrides(self):
        trust_path = os.path.join(self.data_dir, "trust.json")
        if os.path.exists(trust_path):
            try:
                mtime = os.path.getmtime(trust_path)
                if mtime == self._trust_file_mtime:
                    return  # No changes
                with open(trust_path) as f:
                    data = json.load(f)
                self._trust_overrides.clear()
                for peer_id, tier_value in data.items():
                    if isinstance(tier_value, str):
                        self._trust_overrides[peer_id] = TrustTier[tier_value.upper()]
                    else:
                        self._trust_overrides[peer_id] = TrustTier(tier_value)
                self._trust_file_mtime = mtime
            except (json.JSONDecodeError, IOError, KeyError, ValueError) as e:
                logger.warning(f"Failed to load trust.json: {e}")

    def _save_trust_overrides(self):
        trust_path = os.path.join(self.data_dir, "trust.json")
        data = {peer_id: tier.name.lower() for peer_id, tier in self._trust_overrides.items()}
        try:
            with open(trust_path, "w") as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            logger.error(f"Failed to save trust.json: {e}")

    # ── Trust tier queries ──

    def get_trust_tier(self, peer_id: str) -> int:
        """Get a peer's trust tier. Defaults to UNTRUSTED.

        Hot-reloads trust.json if the file has been modified since last read.
        """
        self._load_trust_overrides()
        return int(self._trust_overrides.get(peer_id, TrustTier.UNTRUSTED))

    def set_trust_tier(self, peer_id: str, tier: int):
        """Set a peer's trust tier (in memory only)."""
        self._trust_overrides[peer_id] = TrustTier(tier)

    def set_trust_override(self, peer_id: str, tier: int):
        """Set a peer's trust tier and persist to disk."""
        self._trust_overrides[peer_id] = TrustTier(tier)
        self._save_trust_overrides()

    def remove_trust_override(self, peer_id: str):
        if peer_id in self._trust_overrides:
            del self._trust_overrides[peer_id]
            self._save_trust_overrides()

    @property
    def all_tiers(self) -> Dict[str, str]:
        return {peer_id: tier.name for peer_id, tier in self._trust_overrides.items()}

    # ── Message permission ──

    def is_message_allowed(self, peer_id: str, msg_type: str) -> bool:
        """Check if a peer is allowed to send a given message type."""
        peer_tier = self.get_trust_tier(peer_id)
        required = MIN_TRUST_BY_MSG_TYPE.get(msg_type, TrustTier.PRIVILEGED)
        return peer_tier >= required

    def get_rejection_info(self, peer_id: str, msg_type: str) -> Optional[dict]:
        """If message not allowed, return rejection info. None if allowed."""
        peer_tier = self.get_trust_tier(peer_id)
        required = MIN_TRUST_BY_MSG_TYPE.get(msg_type, TrustTier.PRIVILEGED)
        if peer_tier >= required:
            return None
        return {
            "code": "TRUST_TIER_TOO_LOW",
            "message": f"Message type '{msg_type}' requires trust tier >= {required}, "
                       f"but peer has tier {peer_tier}",
            "peer_tier": peer_tier,
            "required_tier": required,
        }

    # ── Skill visibility ──

    def filter_skills_for_peer(self, peer_id: str,
                                skills: List[dict]) -> List[dict]:
        """Filter skill list to only those visible to this peer.

        A skill is visible when peer_tier >= skill.min_trust_tier.
        Default min_trust_tier is KNOWN(1), so UNTRUSTED sees nothing by default.
        """
        peer_tier = self.get_trust_tier(peer_id)
        visible = []
        for skill in skills:
            min_tier = int(skill.get("min_trust_tier", TrustTier.KNOWN))
            if peer_tier >= min_tier:
                visible.append(skill)
        return visible

    # ── Trust request handling ──

    def handle_trust_request(self, peer_id: str, requested_tier: int,
                              reason: str = "") -> dict:
        """Process a trust request. Returns grant or deny payload."""
        if self.auto_accept_trust and requested_tier <= self.max_auto_accept_tier:
            self.set_trust_override(peer_id, requested_tier)
            return {
                "granted": True,
                "granted_tier": requested_tier,
                "reason": "auto-accepted",
            }
        else:
            return {
                "granted": False,
                "requested_tier": requested_tier,
                "reason": reason or "manual approval required",
            }
