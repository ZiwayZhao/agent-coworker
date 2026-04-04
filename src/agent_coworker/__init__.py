"""agent-coworker — Peer-to-peer AI agent collaboration over XMTP."""

__version__ = "0.6.1"

from .agent import Agent, Group

# Re-export the skill decorator helper for convenience
def skill(name: str, description: str = "", **kwargs):
    """Standalone skill decorator (requires an Agent instance to register).

    Prefer using @agent.skill() instead.
    """
    raise RuntimeError(
        "Use @agent.skill() instead of the standalone @skill() decorator. "
        "Example: agent = Agent('my-bot'); @agent.skill('echo')"
    )

__all__ = ["Agent", "Group", "skill", "__version__"]
