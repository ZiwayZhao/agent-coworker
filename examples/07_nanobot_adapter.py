#!/usr/bin/env python3
"""Nanobot ↔ CoWorker adapter — expose nanobot skills via CoWorker protocol.

This adapter wraps a nanobot agent as a CoWorker peer, so any CoWorker agent
can discover and call nanobot skills over XMTP (wallet-to-wallet, E2E encrypted).

Architecture:
    nanobot agent (local)
        ↕ Python function calls
    CoWorker adapter (this file)
        ↕ XMTP bridge (localhost HTTP)
    Remote CoWorker peer (any network)

Usage:
    # 1. Install both
    pip install nanobot-ai agent-coworker

    # 2. Init CoWorker (generates wallet, installs XMTP bridge)
    coworker init --name my-nanobot

    # 3. Run adapter
    python 07_nanobot_adapter.py

    # 4. Remote peer can now discover and call your nanobot skills:
    #    peer = agent.connect('0xYOUR_WALLET...')
    #    result = agent.call('0xYOUR_WALLET...', 'memory', {'query': 'recent notes'})
"""
import sys
import os

from agent_coworker import Agent


def load_nanobot_skills(agent: Agent):
    """Discover nanobot workspace skills and register them as CoWorker skills.

    Reads SKILL.md files from ~/.nanobot/workspace/skills/ and registers
    each as a CoWorker skill that forwards execution to nanobot.
    """
    import subprocess
    import json
    import yaml
    from pathlib import Path

    workspace = Path.home() / ".nanobot" / "workspace" / "skills"
    if not workspace.exists():
        print(f"  No nanobot workspace found at {workspace}")
        print(f"  Run 'nanobot' first to initialize, then restart this adapter.")
        return

    for skill_dir in workspace.iterdir():
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        # Parse YAML frontmatter
        content = skill_md.read_text()
        if not content.startswith("---"):
            continue
        parts = content.split("---", 2)
        if len(parts) < 3:
            continue

        try:
            meta = yaml.safe_load(parts[1])
        except Exception:
            continue

        skill_name = meta.get("name", skill_dir.name)
        description = meta.get("description", f"Nanobot skill: {skill_name}")

        # Check if skill has required dependencies
        nanobot_meta = meta.get("metadata", {})
        if isinstance(nanobot_meta, dict):
            nb = nanobot_meta.get("nanobot", nanobot_meta.get("openclaw", {}))
            requires = nb.get("requires", {}) if isinstance(nb, dict) else {}
        else:
            requires = {}

        # Register as CoWorker skill (closure captures skill_name)
        def make_handler(sname):
            def handler(**kwargs):
                """Forward to nanobot skill via subprocess."""
                # Find the skill's scripts directory
                scripts_dir = workspace / sname / "scripts"
                if scripts_dir.exists():
                    # Look for a main.py or run.py
                    for entry in ["main.py", "run.py", f"{sname}.py"]:
                        script = scripts_dir / entry
                        if script.exists():
                            result = subprocess.run(
                                [sys.executable, str(script), json.dumps(kwargs)],
                                capture_output=True, text=True, timeout=30,
                            )
                            try:
                                return json.loads(result.stdout)
                            except json.JSONDecodeError:
                                return {"output": result.stdout.strip(), "stderr": result.stderr.strip()}

                # Fallback: return skill metadata
                return {
                    "skill": sname,
                    "input": kwargs,
                    "note": f"Skill '{sname}' registered but no executable script found",
                }
            return handler

        agent.skill(
            skill_name,
            description=description,
            min_trust_tier=1,  # Require KNOWN trust by default
        )(make_handler(skill_name))

        print(f"  ✓ Registered nanobot skill: {skill_name}")


def main():
    agent = Agent("nanobot-bridge")

    print(f"\n  Nanobot ↔ CoWorker Adapter")
    print(f"  Loading nanobot workspace skills...\n")

    # Try loading nanobot skills
    try:
        load_nanobot_skills(agent)
    except ImportError:
        print("  ⚠ PyYAML not installed. Install with: pip install pyyaml")
        print("  Registering demo skill instead.\n")

        @agent.skill("echo", description="Echo input (demo)")
        def echo(text=""):
            return {"echo": text}

    # Also register a meta-skill for listing capabilities
    @agent.skill("list_skills", description="List all available nanobot skills", min_trust_tier=0)
    def list_skills():
        return {"skills": agent.executor.skill_names}

    print(f"\n  Starting CoWorker agent with nanobot skills...")
    print(f"  Share your wallet address for peers to connect.\n")

    agent.serve()


if __name__ == "__main__":
    main()
