#!/usr/bin/env python3
"""OpenClaw ↔ CoWorker adapter — expose OpenClaw skills via CoWorker protocol.

This adapter wraps an OpenClaw agent as a CoWorker peer, so any CoWorker agent
can discover and call OpenClaw skills over XMTP (wallet-to-wallet, E2E encrypted).

Why? OpenClaw skills are powerful but only accessible within a single agent.
CoWorker lets two agents collaborate privately — without exposing skills to a
public marketplace (unlike ClawHub).

Architecture:
    OpenClaw agent (local)
        ↕ Python function calls / subprocess
    CoWorker adapter (this file)
        ↕ XMTP bridge (localhost HTTP)
    Remote CoWorker peer (any network)

Usage:
    # 1. Install CoWorker
    pip install agent-coworker

    # 2. Init (generates wallet + XMTP bridge)
    coworker init --name my-claw-agent

    # 3. Run adapter
    python 08_openclaw_adapter.py

    # 4. Share your wallet address with a trusted peer
    coworker invite

    # 5. Peer connects and calls your OpenClaw skills:
    #    peer = agent.connect('0xYOUR_WALLET...')
    #    result = agent.call('0xYOUR_WALLET...', 'memory', {'query': '...'})
"""
import sys
import os
import json
import subprocess
from pathlib import Path

from agent_coworker import Agent


def discover_openclaw_skills() -> list:
    """Scan OpenClaw workspace for installed skills.

    Reads SKILL.md files from ~/.openclaw/workspace/skills/ and returns
    a list of {name, description, path} dicts.
    """
    skills = []
    workspace = Path.home() / ".openclaw" / "workspace" / "skills"
    if not workspace.exists():
        return skills

    for skill_dir in workspace.iterdir():
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        content = skill_md.read_text()
        if not content.startswith("---"):
            continue

        # Parse YAML frontmatter
        parts = content.split("---", 2)
        if len(parts) < 3:
            continue

        try:
            import yaml
            meta = yaml.safe_load(parts[1])
        except Exception:
            # Fallback: basic parsing
            meta = {"name": skill_dir.name}

        skills.append({
            "name": meta.get("name", skill_dir.name),
            "description": meta.get("description", f"OpenClaw skill: {skill_dir.name}"),
            "path": str(skill_dir),
            "triggers": meta.get("triggers", []),
        })

    return skills


def register_openclaw_skills(agent: Agent, skills: list):
    """Register discovered OpenClaw skills as CoWorker skills.

    Each skill becomes callable over XMTP by trusted peers.
    The adapter handles:
    - Skill discovery (via CoWorker discover protocol)
    - Trust-gated access (only KNOWN+ peers can call)
    - E2E encrypted transport (via XMTP)
    """
    for skill_info in skills:
        name = skill_info["name"]
        desc = skill_info["description"]
        path = skill_info["path"]

        def make_handler(skill_path, skill_name):
            def handler(**kwargs):
                """Execute OpenClaw skill scripts."""
                scripts_dir = Path(skill_path) / "scripts"
                if not scripts_dir.exists():
                    return {"skill": skill_name, "status": "no scripts directory"}

                # Try common entry points
                for entry in ["main.py", "run.py", f"{skill_name}.py"]:
                    script = scripts_dir / entry
                    if script.exists():
                        try:
                            result = subprocess.run(
                                [sys.executable, str(script), json.dumps(kwargs)],
                                capture_output=True, text=True, timeout=60,
                            )
                            try:
                                return json.loads(result.stdout)
                            except json.JSONDecodeError:
                                return {
                                    "output": result.stdout.strip()[:2000],
                                    "exit_code": result.returncode,
                                }
                        except subprocess.TimeoutExpired:
                            return {"error": "timeout", "skill": skill_name}

                return {"skill": skill_name, "note": "no executable entry point found"}
            return handler

        agent.skill(
            name,
            description=desc,
            min_trust_tier=1,  # KNOWN+ only (private by default)
        )(make_handler(path, name))

        print(f"  ✓ {name}: {desc}")


def main():
    agent = Agent("openclaw-bridge")

    print(f"\n  OpenClaw ↔ CoWorker Bridge")
    print(f"  ─────────────────────────────────")
    print(f"  Scanning OpenClaw workspace...\n")

    skills = discover_openclaw_skills()

    if skills:
        print(f"  Found {len(skills)} OpenClaw skills:\n")
        register_openclaw_skills(agent, skills)
    else:
        print("  No OpenClaw skills found at ~/.openclaw/workspace/skills/")
        print("  Registering demo skills instead.\n")

        @agent.skill("echo", description="Echo back input")
        def echo(text=""):
            return {"echo": text}

        @agent.skill("summarize", description="Summarize text")
        def summarize(text="", max_words=10):
            words = text.split()[:max_words]
            return {"summary": " ".join(words), "word_count": len(words)}

    # Meta-skill: list what's available
    @agent.skill("capabilities", description="List available skills", min_trust_tier=0)
    def list_caps():
        return {"skills": agent.executor.skill_names, "framework": "openclaw"}

    print(f"\n  Starting CoWorker bridge...")
    print(f"  Your OpenClaw skills are now accessible to trusted peers over XMTP.")
    print(f"  Share your wallet address: coworker invite\n")

    agent.serve()


if __name__ == "__main__":
    main()
