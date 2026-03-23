"""Skill Visibility Control — manages which skills are exposed to peers.

Two-layer model:
  Layer 1: visibility (exposed / hidden / pending_review)
  Layer 2: trust tier (min_trust_tier on each skill)

Final rule:
  visible_to_peer = skill_is_exposed AND peer_tier >= min_trust_tier
"""

import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("coworker.visibility")

# Skill states
STATE_EXPOSED = "exposed"
STATE_HIDDEN = "hidden"
STATE_INHERIT = "inherit"  # V1: resolves to default_visibility

VALID_STATES = {STATE_EXPOSED, STATE_HIDDEN, STATE_INHERIT}

SKILLS_CONFIG_FILE = "skills.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SkillVisibilityConfig:
    """Load, merge, and persist skill visibility configuration."""

    def __init__(self, data_dir: str, agent_id: str = "default"):
        self.data_dir = str(data_dir)
        self.agent_id = agent_id
        self._config_path = os.path.join(self.data_dir, SKILLS_CONFIG_FILE)
        self._data: dict = {}
        self._loaded = False

    @property
    def config_path(self) -> str:
        return self._config_path

    @property
    def exists(self) -> bool:
        return os.path.isfile(self._config_path)

    def load(self) -> dict:
        """Load config from disk. Returns empty dict if not found."""
        if not os.path.isfile(self._config_path):
            self._data = {}
            self._loaded = True
            return self._data

        try:
            with open(self._config_path) as f:
                self._data = json.load(f)
            self._loaded = True
            # Validate version
            version = self._data.get("version")
            if version is not None and version > 1:
                logger.warning("skills.json version %s > 1, entering safe mode", version)
                self._data = {}
            # Validate and fix illegal state combinations
            self._sanitize()
            return self._data
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.warning("Failed to load %s: %s", self._config_path, e)
            self._data = {}
            self._loaded = True
            return self._data

    def _sanitize(self):
        """Fix illegal state combinations."""
        skills = self._data.get("skills", {})
        for name, cfg in skills.items():
            state = cfg.get("state", STATE_HIDDEN)
            if state not in VALID_STATES:
                logger.warning("Invalid state '%s' for skill '%s', correcting to hidden", state, name)
                cfg["state"] = STATE_HIDDEN
            # pending_review=true + exposed is illegal
            if cfg.get("pending_review") and cfg.get("state") == STATE_EXPOSED:
                logger.warning("Illegal: pending_review=true + exposed for '%s', correcting to hidden", name)
                cfg["state"] = STATE_HIDDEN

    def save(self) -> bool:
        """Atomically save config to disk with fsync for durability."""
        os.makedirs(self.data_dir, exist_ok=True)
        data = {
            "version": 1,
            "agent_id": self.agent_id,
            "skills": self._data.get("skills", {}),
        }
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(dir=self.data_dir, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                fd = None  # os.fdopen takes ownership
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._config_path)
            # fsync directory for rename durability
            dir_fd = os.open(self.data_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            return True
        except (IOError, OSError) as e:
            logger.error("Failed to save %s: %s", self._config_path, e)
            if fd is not None:
                os.close(fd)
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            return False

    def get_skills(self) -> Dict[str, dict]:
        """Return skills dict from config."""
        if not self._loaded:
            self.load()
        return dict(self._data.get("skills", {}))

    def get_state(self, skill_name: str) -> Optional[str]:
        """Get the state of a specific skill. Returns None if not configured."""
        if not self._loaded:
            self.load()
        skill = self._data.get("skills", {}).get(skill_name)
        if skill is None:
            return None
        return skill.get("state", STATE_HIDDEN)

    def set_state(self, skill_name: str, state: str, clear_pending: bool = True):
        """Set the visibility state of a skill."""
        if state not in VALID_STATES:
            raise ValueError(f"Invalid state: {state}. Must be one of {VALID_STATES}")
        if not self._loaded:
            self.load()
        if "skills" not in self._data:
            self._data["skills"] = {}
        now = _now_iso()
        if skill_name not in self._data["skills"]:
            self._data["skills"][skill_name] = {
                "state": state,
                "discovered_at": now,
                "last_seen_at": now,
            }
        else:
            self._data["skills"][skill_name]["state"] = state
            self._data["skills"][skill_name]["last_seen_at"] = now
        if clear_pending:
            self._data["skills"][skill_name].pop("pending_review", None)

    def is_pending_review(self, skill_name: str) -> bool:
        if not self._loaded:
            self.load()
        skill = self._data.get("skills", {}).get(skill_name)
        return bool(skill and skill.get("pending_review"))

    def merge_discovered_skills(self, registered_skill_names: List[str]) -> dict:
        """Compare registered skills against config. Returns merge result.

        Returns:
            {
                "new_skills": ["skill1", "skill2"],   # not in config
                "missing_skills": ["old_skill"],       # in config but not registered
                "existing_skills": ["skill3"],         # in both
                "has_config": bool,                    # whether config existed
            }
        """
        if not self._loaded:
            self.load()

        has_config = self.exists  # file existence, not content emptiness
        configured_names = set(self._data.get("skills", {}).keys())
        registered_set = set(registered_skill_names)

        new_skills = sorted(registered_set - configured_names)
        missing_skills = sorted(configured_names - registered_set)
        existing_skills = sorted(registered_set & configured_names)

        # Update last_seen_at for existing skills
        now = _now_iso()
        for name in existing_skills:
            self._data["skills"][name]["last_seen_at"] = now

        # Add new skills as pending_review (hidden)
        if has_config and new_skills:
            if "skills" not in self._data:
                self._data["skills"] = {}
            for name in new_skills:
                self._data["skills"][name] = {
                    "state": STATE_HIDDEN,
                    "pending_review": True,
                    "discovered_at": now,
                    "last_seen_at": now,
                }

        return {
            "new_skills": new_skills,
            "missing_skills": missing_skills,
            "existing_skills": existing_skills,
            "has_config": has_config,
        }

    def set_all_exposed(self, skill_names: List[str]):
        """Mark all given skills as exposed (used for first-run confirmation)."""
        now = _now_iso()
        if "skills" not in self._data:
            self._data["skills"] = {}
        for name in skill_names:
            self._data["skills"][name] = {
                "state": STATE_EXPOSED,
                "discovered_at": now,
                "last_seen_at": now,
            }

    def set_all_hidden(self, skill_names: List[str]):
        """Mark all given skills as hidden."""
        now = _now_iso()
        if "skills" not in self._data:
            self._data["skills"] = {}
        for name in skill_names:
            self._data["skills"][name] = {
                "state": STATE_HIDDEN,
                "discovered_at": now,
                "last_seen_at": now,
            }

    def get_exposed_set(self, default_visibility: str = STATE_EXPOSED) -> set:
        """Compute the final set of exposed skill names.

        Args:
            default_visibility: What 'inherit' resolves to.

        Returns:
            Set of skill names that should be exposed.
        """
        if not self._loaded:
            self.load()
        exposed = set()
        for name, cfg in self._data.get("skills", {}).items():
            state = cfg.get("state", STATE_HIDDEN)
            if cfg.get("pending_review"):
                continue  # pending_review always means hidden
            if state == STATE_EXPOSED:
                exposed.add(name)
            elif state == STATE_INHERIT:
                if default_visibility == STATE_EXPOSED:
                    exposed.add(name)
            # STATE_HIDDEN: not added
        return exposed

    def reset(self):
        """Reset all configuration."""
        self._data = {}
        try:
            os.unlink(self._config_path)
        except OSError:
            pass


def run_first_time_guide(skill_names: List[str], config: SkillVisibilityConfig) -> set:
    """Interactive first-run skill visibility guide.

    Returns set of exposed skill names.
    """
    if not skill_names:
        print("\n  No skills detected. Nothing to configure.")
        return set()

    print(f"\n  ── Skill Review {'─' * 40}")
    print("\n  Skills are hidden by default until you review them.\n")
    print("  Detected skills:")
    for i, name in enumerate(skill_names, 1):
        print(f"    {i}. {name}")

    print(f"\n  Expose all {len(skill_names)} skills to peers? [Y/n]: ", end="", flush=True)

    try:
        choice = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        choice = "n"

    if choice in ("", "y", "yes"):
        config.set_all_exposed(skill_names)
        if config.save():
            print(f"\n  ✓ All {len(skill_names)} skills exposed.")
            print(f"  Saved to {config.config_path}")
        else:
            print(f"\n  ⚠ Could not save config. Skills exposed for this session only.")
        print(f"\n  Change later:   coworker skills configure")
        print(f"  Preview:        coworker skills preview --peer-tier KNOWN")
        return set(skill_names)
    else:
        config.set_all_hidden(skill_names)
        if config.save():
            print(f"\n  All skills remain hidden.")
            print(f"  Saved to {config.config_path}")
        else:
            print(f"\n  ⚠ Could not save config.")
        print(f"\n  Configure later:  coworker skills configure")
        return set()


def print_new_skill_reminder(new_skills: List[str]):
    """Print a reminder about new unreviewed skills."""
    if not new_skills:
        return
    print(f"\n  ── New Skills Detected {'─' * 34}")
    print(f"\n  {len(new_skills)} new skill(s) require review (currently hidden):")
    for name in new_skills:
        print(f"    - {name}")
    print(f"\n  Review:   coworker skills configure")
    print(f"  Preview:  coworker skills preview --peer-tier KNOWN")


def compute_effective_exposed(
    registered_skill_names: List[str],
    config: SkillVisibilityConfig,
    expose_skills_override: Optional[List[str]] = None,
) -> set:
    """Compute the final set of exposed skill names.

    Priority: expose_skills_override > config > default

    Args:
        registered_skill_names: Skills registered via @agent.skill()
        config: Loaded visibility config
        expose_skills_override: From serve(expose_skills=[...])

    Returns:
        Set of skill names that should be exposed.
    """
    registered_set = set(registered_skill_names)

    # Runtime override takes precedence
    if expose_skills_override is not None:
        override_set = set(expose_skills_override)
        # Warn about skills in override that don't exist
        missing = override_set - registered_set
        for name in sorted(missing):
            logger.warning("expose_skills references unknown skill: %s", name)
        return override_set & registered_set

    # Use persistent config
    if config.exists or config._loaded:
        exposed = config.get_exposed_set()
        # Only return skills that are actually registered
        return exposed & registered_set

    # No config, no override — nothing exposed (will trigger first-run guide)
    return set()
