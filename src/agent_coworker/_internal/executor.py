"""AgentFax Task Executor — plugin-based skill execution framework."""

import json
import logging
import os
import time
import traceback
from typing import Callable, Dict, Optional, Any, List

logger = logging.getLogger("agentfax.executor")


class SkillDefinition:
    """Metadata about a registered skill."""

    def __init__(
        self, name: str, func: Callable, description: str = "",
        input_schema: dict = None, output_schema: dict = None,
        min_trust_tier: int = 1, max_context_privacy_tier: str = "L1_PUBLIC",
    ):
        self.name = name
        self.func = func
        self.description = description or f"Skill: {name}"
        self.input_schema = input_schema or {}
        self.output_schema = output_schema or {}
        self.min_trust_tier = min_trust_tier
        self.max_context_privacy_tier = max_context_privacy_tier

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "min_trust_tier": self.min_trust_tier,
            "max_context_privacy_tier": self.max_context_privacy_tier,
        }


class TaskExecutor:
    """Registers and executes skills for incoming task requests."""

    def __init__(self):
        self._skills: Dict[str, SkillDefinition] = {}
        self._stats = {"executed": 0, "succeeded": 0, "failed": 0}

    def skill(
        self, name: str, description: str = "", input_schema: dict = None,
        output_schema: dict = None, min_trust_tier: int = 1,
        max_context_privacy_tier: str = "L1_PUBLIC",
    ):
        """Decorator to register a skill function."""
        def decorator(func):
            skill_def = SkillDefinition(
                name=name, func=func, description=description,
                input_schema=input_schema, output_schema=output_schema,
                min_trust_tier=min_trust_tier,
                max_context_privacy_tier=max_context_privacy_tier,
            )
            self._skills[name] = skill_def
            return func
        return decorator

    def register_skill(
        self, name: str, func: Callable, description: str = "",
        input_schema: dict = None, output_schema: dict = None,
        min_trust_tier: int = 1, max_context_privacy_tier: str = "L1_PUBLIC",
    ):
        """Register a skill function (non-decorator version)."""
        skill_def = SkillDefinition(
            name=name, func=func, description=description,
            input_schema=input_schema, output_schema=output_schema,
            min_trust_tier=min_trust_tier,
            max_context_privacy_tier=max_context_privacy_tier,
        )
        self._skills[name] = skill_def

    def execute(self, skill_name: str, input_data: Any) -> dict:
        """Execute a skill with given input."""
        self._stats["executed"] += 1
        skill_def = self._skills.get(skill_name)
        if not skill_def:
            self._stats["failed"] += 1
            return {
                "success": False,
                "error": f"Unknown skill: {skill_name}",
            }

        start = time.time()
        try:
            # Unpack dict as keyword arguments if input is a dict
            if isinstance(input_data, dict):
                import inspect
                sig = inspect.signature(skill_def.func)
                params = sig.parameters
                # Filter to only recognized parameters (avoid unexpected keyword args)
                has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
                if has_var_keyword:
                    filtered = input_data
                else:
                    filtered = {k: v for k, v in input_data.items() if k in params}
                result = skill_def.func(**filtered)
            else:
                result = skill_def.func(input_data)
            duration_ms = (time.time() - start) * 1000
            self._stats["succeeded"] += 1
            return {"success": True, "result": result, "duration_ms": round(duration_ms, 1)}
        except Exception as e:
            duration_ms = (time.time() - start) * 1000
            self._stats["failed"] += 1
            return {
                "success": False, "error": str(e),
                "traceback": traceback.format_exc(),
                "duration_ms": round(duration_ms, 1),
            }

    def has_skill(self, name: str) -> bool:
        return name in self._skills

    def list_skills(self) -> List[dict]:
        return [s.to_dict() for s in self._skills.values()]

    def list_skills_for_tier(self, peer_tier: int = 0,
                             exposed_set: set = None) -> List[dict]:
        """Return only skills visible to a given trust tier and visibility policy.

        Two-layer filtering:
          Layer 1: visibility — skill must be in exposed_set (if provided)
          Layer 2: trust tier — peer_tier >= skill.min_trust_tier

        Args:
            peer_tier: The peer's trust tier level.
            exposed_set: Set of skill names that are exposed. If None, all
                        registered skills pass the visibility check.
        """
        result = []
        for s in self._skills.values():
            # Layer 1: visibility filter
            if exposed_set is not None and s.name not in exposed_set:
                continue
            # Layer 2: trust tier filter
            if peer_tier < (s.min_trust_tier if s.min_trust_tier is not None else 1):
                continue
            result.append(s.to_dict())
        return result

    @property
    def skill_names(self) -> List[str]:
        return list(self._skills.keys())

    def get_skill(self, name: str) -> Optional[SkillDefinition]:
        return self._skills.get(name)

    @property
    def stats(self) -> dict:
        return dict(self._stats)


def register_builtin_skills(executor: TaskExecutor):
    """Register built-in demonstration skills."""

    @executor.skill("echo", description="Echo input back unchanged")
    def echo(input_data):
        return {"echo": input_data}

    @executor.skill("ping_skill", description="Simple liveness check skill")
    def ping_skill(input_data):
        return {"status": "alive", "timestamp": time.time(), "received": input_data}

    @executor.skill(
        "reverse", description="Reverse a text string",
        input_schema={"text": "string"}, output_schema={"reversed": "string"},
    )
    def reverse(input_data):
        text = input_data if isinstance(input_data, str) else str(input_data.get("text", ""))
        return {"reversed": text[::-1]}

    @executor.skill(
        "word_count", description="Count words in text",
        input_schema={"text": "string"}, output_schema={"count": "integer", "words": "list"},
    )
    def word_count(input_data):
        text = input_data if isinstance(input_data, str) else str(input_data.get("text", ""))
        words = text.split()
        return {"count": len(words), "words": words}
