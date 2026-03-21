"""AgentFax Peer Manager — address book for known agents."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any


class PeerManager:
    """Manages the local address book of known peers."""

    def __init__(self, data_dir: str):
        self.data_dir = str(Path(data_dir).expanduser())
        self.peers_file = os.path.join(self.data_dir, "peers.json")
        self._peers: Dict[str, dict] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.peers_file):
            try:
                with open(self.peers_file) as f:
                    self._peers = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._peers = {}

    def _save(self):
        with open(self.peers_file, "w") as f:
            json.dump(self._peers, f, indent=2, ensure_ascii=False)

    def update_seen(self, sender_id: str, wallet: str = None, latency_ms: float = None):
        if sender_id not in self._peers:
            self._peers[sender_id] = {}
        peer = self._peers[sender_id]
        peer["last_seen"] = datetime.now(timezone.utc).isoformat()
        peer["seen_count"] = peer.get("seen_count", 0) + 1
        if wallet:
            peer["wallet"] = wallet.lower()
        if latency_ms is not None:
            peer["latency_ms"] = round(latency_ms, 1)
            prev_avg = peer.get("avg_latency_ms", latency_ms)
            count = peer.get("latency_samples", 0)
            peer["avg_latency_ms"] = round((prev_avg * count + latency_ms) / (count + 1), 1)
            peer["latency_samples"] = count + 1
        self._save()

    def update_capabilities(self, sender_id: str, wallet: str = None, capabilities: dict = None):
        if sender_id not in self._peers:
            self._peers[sender_id] = {}
        peer = self._peers[sender_id]
        if wallet:
            peer["wallet"] = wallet.lower()
        if capabilities:
            peer["capabilities"] = capabilities
            peer["skills"] = [
                s.get("skill_name") or s.get("name")
                for s in capabilities.get("skills", [])
                if s.get("skill_name") or s.get("name")
            ]
            peer["capabilities_updated"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def get(self, sender_id: str) -> Optional[dict]:
        return self._peers.get(sender_id)

    def get_by_wallet(self, wallet: str) -> Optional[dict]:
        wallet_lower = wallet.lower()
        for name, peer in self._peers.items():
            if peer.get("wallet") == wallet_lower:
                return {**peer, "name": name}
        return None

    def find_by_skill(self, skill_name: str) -> List[dict]:
        results = []
        for name, peer in self._peers.items():
            if skill_name in peer.get("skills", []):
                results.append({**peer, "name": name})
        return results

    def get_online(self, timeout_seconds: int = 120) -> List[dict]:
        now = datetime.now(timezone.utc)
        results = []
        for name, peer in self._peers.items():
            last_seen = peer.get("last_seen")
            if last_seen:
                try:
                    ts = datetime.fromisoformat(last_seen)
                    age = (now - ts).total_seconds()
                    if age <= timeout_seconds:
                        results.append({**peer, "name": name, "age_seconds": age})
                except (ValueError, TypeError):
                    pass
        return sorted(results, key=lambda p: p.get("age_seconds", 999))

    def list_all(self) -> Dict[str, dict]:
        return dict(self._peers)

    def remove(self, sender_id: str):
        if sender_id in self._peers:
            del self._peers[sender_id]
            self._save()

    def set_skill_cache(self, skill_cache):
        self._skill_cache = skill_cache

    def get_skill_cards(self, sender_id: str) -> list:
        cache = getattr(self, "_skill_cache", None)
        if cache:
            return cache.get_cards(sender_id)
        return []

    def find_by_skill_card(self, skill_name: str) -> list:
        cache = getattr(self, "_skill_cache", None)
        if not cache:
            return self.find_by_skill(skill_name)
        results = []
        for entry in cache.find_by_skill(skill_name):
            peer_id = entry["peer_id"]
            peer_info = self._peers.get(peer_id, {})
            results.append({**peer_info, "name": peer_id, "card": entry["card"]})
        return results

    def count(self) -> int:
        return len(self._peers)
