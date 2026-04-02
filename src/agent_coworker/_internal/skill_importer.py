"""SKILL.md Importer — parse Claude Code / AgentSkills format into CoWorker SkillManifest.

Supports the standard SKILL.md format with YAML frontmatter:
```
---
description: What this skill does
when_to_use: When the agent should use this skill
allowed-tools: [Tool1, Tool2]
---
# Skill content (the actual instructions)
```

The SKILL.md body (instructions/prompts) is treated as PRIVATE —
it stays on the provider's machine and is never transmitted via CoWorker protocol.
Only the frontmatter metadata (description, when_to_use, schema) is shared.
"""

import os
import re
import hashlib
import logging
from pathlib import Path
from typing import Optional, Dict, Tuple

from .skill_manifest import SkillManifest

logger = logging.getLogger("coworker.skill_importer")

# Standard SKILL.md frontmatter fields (from Claude Code Ch20)
KNOWN_FIELDS = {
    "description", "when_to_use", "allowed-tools", "arguments",
    "context", "agent", "effort", "model", "disable-model-invocation",
    "user-invocable", "hooks", "paths", "version",
}


def parse_skill_md(path: str) -> Tuple[dict, str]:
    """Parse a SKILL.md file into frontmatter dict + body string.

    Returns:
        (frontmatter_dict, body_string)
    """
    skill_path = Path(path)

    # Find SKILL.md
    if skill_path.is_dir():
        skill_file = skill_path / "SKILL.md"
        if not skill_file.exists():
            raise FileNotFoundError(f"No SKILL.md found in {path}")
    elif skill_path.is_file():
        skill_file = skill_path
    else:
        raise FileNotFoundError(f"Path not found: {path}")

    content = skill_file.read_text(encoding="utf-8")

    # Split frontmatter and body
    frontmatter = {}
    body = content

    # YAML frontmatter between --- markers
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
    if fm_match:
        fm_text = fm_match.group(1)
        body = fm_match.group(2)

        # Simple YAML parser (avoid pyyaml dependency)
        for line in fm_text.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip()
                # Handle list values like [Tool1, Tool2]
                if value.startswith("[") and value.endswith("]"):
                    value = [v.strip().strip('"').strip("'")
                             for v in value[1:-1].split(",") if v.strip()]
                # Handle boolean
                elif value.lower() in ("true", "false"):
                    value = value.lower() == "true"
                # Handle quoted strings
                elif value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                frontmatter[key] = value

    return frontmatter, body


def infer_input_schema(body: str, frontmatter: dict) -> dict:
    """Infer input schema from SKILL.md content.

    Looks for argument patterns like $arg_name or {arg_name}.
    """
    schema = {}

    # Check frontmatter arguments field
    args = frontmatter.get("arguments", [])
    if isinstance(args, list):
        for arg in args:
            if isinstance(arg, str):
                schema[arg] = "str"
            elif isinstance(arg, dict):
                for k, v in arg.items():
                    schema[k] = str(v) if v else "str"

    # Scan body for $arg patterns
    arg_refs = re.findall(r'\$([a-zA-Z_][a-zA-Z0-9_]*)', body)
    for arg in arg_refs:
        if arg not in schema and arg not in ("CLAUDE_SKILL_DIR", "CLAUDE_SESSION_ID"):
            schema[arg] = "str"

    # If no schema found, default to generic text input
    if not schema:
        schema = {"input": "str"}

    return schema


def skill_md_to_manifest(
    path: str,
    name: str = "",
    override_when_to_use: str = "",
    override_version: str = "",
) -> SkillManifest:
    """Convert a SKILL.md file/directory into a CoWorker SkillManifest.

    The SKILL.md body (prompts/instructions) is NOT included in the manifest.
    It stays private on the provider's machine.

    Args:
        path: Path to SKILL.md file or directory containing it.
        name: Override skill name (default: inferred from directory name).
        override_when_to_use: Override when_to_use from frontmatter.
        override_version: Override version.

    Returns:
        SkillManifest with public metadata only.
    """
    frontmatter, body = parse_skill_md(path)

    # Infer name from directory
    p = Path(path)
    if not name:
        if p.is_dir():
            name = p.name
        else:
            name = p.parent.name if p.name == "SKILL.md" else p.stem
    # Sanitize name
    name = re.sub(r'[^a-zA-Z0-9_-]', '_', name).lower()

    # Extract metadata
    description = frontmatter.get("description", f"Wrapped skill: {name}")
    when_to_use = override_when_to_use or frontmatter.get("when_to_use", "")
    version = override_version or frontmatter.get("version", "1.0.0")
    context = frontmatter.get("context", "inline")
    if context not in ("inline", "fork"):
        context = "inline"

    # Infer input schema
    input_schema = infer_input_schema(body, frontmatter)

    # Body hash for integrity (not exposed to peers)
    body_hash = hashlib.sha256(body.encode()).hexdigest()[:16]

    manifest = SkillManifest(
        name=name,
        description=description,
        when_to_use=when_to_use,
        version=version,
        category="compute",  # wrapped skills are typically compute
        input_schema=input_schema,
        output_schema={"result": "str"},  # generic for wrapped skills
        min_trust_tier=1,
        visibility="public",
        context=context,
        origin_type="wrapped_skill_md",
        wrapped_from=str(p.resolve()),
    )

    logger.info("Imported SKILL.md: %s (hash=%s, args=%s)",
                name, body_hash, list(input_schema.keys()))
    return manifest


def scan_skills_directory(directory: str) -> list:
    """Scan a directory for SKILL.md files and return list of manifests."""
    results = []
    root = Path(directory)
    if not root.is_dir():
        return results

    # Look for SKILL.md in immediate subdirectories
    for child in sorted(root.iterdir()):
        if child.is_dir():
            skill_file = child / "SKILL.md"
            if skill_file.exists():
                try:
                    manifest = skill_md_to_manifest(str(child))
                    results.append(manifest)
                except Exception as e:
                    logger.warning("Failed to import %s: %s", child, e)

    # Also check if root itself has SKILL.md
    if (root / "SKILL.md").exists():
        try:
            manifest = skill_md_to_manifest(str(root))
            results.append(manifest)
        except Exception as e:
            logger.warning("Failed to import %s: %s", root, e)

    return results
