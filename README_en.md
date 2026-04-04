<a name="readme-top"></a>

<p align="right">
  <b>English</b> | <a href="./README.md">中文</a>
</p>

<div align="center">

<img src="docs/assets/logo.png" alt="CoWorker Logo" width="120"/>

# CoWorker Protocol

**Skill-as-API: Call skills across the internet without exposing code.**

<br/>

<a href="https://pypi.org/project/agent-coworker/"><img src="https://img.shields.io/pypi/v/agent-coworker?style=for-the-badge&color=000000" alt="PyPI"></a>
&nbsp;
<a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10+-000000?style=for-the-badge" alt="Python 3.10+"></a>
&nbsp;
<img src="https://img.shields.io/badge/deps-zero-000000?style=for-the-badge" alt="Zero deps">
&nbsp;
<a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-000000?style=for-the-badge" alt="MIT"></a>

<br/><br/>

MCP connects agents to tools. A2A connects agents inside enterprises.<br/>
**CoWorker connects agents across the open internet — peers only see input/output schema, never your code, prompts, or logic.**

</div>

---

## The Problem

When you share a SKILL.md, you share everything — your methodology, prompts, scoring rules, and domain knowledge. Anyone who copies the file owns your work.

Anti-distillation tools try to solve this by poisoning the content. CoWorker takes a different approach: **your skill runs on your machine. Callers only see results.**

```bash
coworker serve ./my-skill/
# Your SKILL.md stays on your machine
# Callers interact via XMTP (E2E encrypted)
# They see: name, description, input/output schema
# They DON'T see: your code, prompts, methodology
```

---

## How It Works

### One command: SKILL.md → live API

```bash
export DEEPSEEK_API_KEY=sk-xxx   # Your LLM key (runs on YOUR machine)
coworker serve ./my-skill/       # Parses SKILL.md, starts serving
```

```
Your machine                          Caller
┌─────────────────────┐            ┌──────────────────┐
│  SKILL.md (private)  │            │                  │
│  + LLM API key       │  ←XMTP→  │  Sees only:      │
│  + Your methodology  │  E2E enc  │  name, schema    │
│  + Your knowledge    │            │  and results     │
└─────────────────────┘            └──────────────────┘
```

### Four layers of protection

| Layer | What | Effect |
|-------|------|--------|
| **Skill-as-API** | Code runs on your machine | Caller only sees results |
| **Trust Tiers** | UNTRUSTED → KNOWN → INTERNAL → PRIVILEGED | You control who can call |
| **Auto-downgrade** | Trust revoked after OKR completion | Collaboration doesn't become permanent access |
| **Skill Hiding** | Hidden skills return "Unknown skill" | Caller can't tell they exist |

### SKILL.md compatibility

CoWorker wraps any standard SKILL.md (Claude Code, AgentSkills format):

```bash
coworker wrap ./colleague-skill/   # Preview what peers would see
coworker serve ./colleague-skill/  # Serve it as a protected API
```

The SKILL.md body (your prompts, instructions, logic) is captured in a closure and used as the LLM system prompt. It is **never transmitted** over the network.

### MCP Bridge

Expose your skills as MCP tools for Claude Code / Cursor:

```bash
coworker mcp serve                 # Start MCP server
coworker mcp test                  # Self-test
```

### AgentCard Fast Reconnect

First connection: 30-60s (XMTP channel establishment). Repeat connections: **~2s** (cached AgentCard with schema hash validation).

### Auto-routing with `when_to_use`

```python
@agent.skill("analyze",
             description="Industry analysis",
             when_to_use="When the caller needs sector analysis with risk assessment",
             category="compute")
def analyze(topic): ...
```

Caller's LLM sees `when_to_use` during discovery and auto-decides whether to delegate.

### Trust Decay

Failed calls automatically downgrade trust:
- 3 consecutive failures → downgrade 1 tier
- 10 cumulative failures → downgrade to UNTRUSTED
- Success resets consecutive count (not cumulative)

### Skill Versioning

```python
@agent.skill("translate", version="2.0.0")
def translate(text, lang): ...
```

Callers can pin to specific versions. Version mismatch returns a clear error with the available version.

### Async Delegation

Send tasks to offline peers. Results delivered when they come online.

```bash
coworker request <invite> analyze --input '{"topic": "AI agents"}'
coworker tasks                    # Check status
coworker result <task_id>         # Get result
```

---

## Quick Start

```bash
pip install agent-coworker
coworker init --name my-agent
coworker bridge start
coworker demo                     # Connect to live demo bot
```

> **First connection note:** XMTP needs 30-60s to establish an encrypted channel between two agents that have never communicated. Subsequent calls are fast (1-3s).

### Write your own agent

```python
from agent_coworker import Agent

agent = Agent("my-bot")

@agent.skill("summarize",
             description="Summarize text",
             when_to_use="When caller needs text summarized",
             input_schema={"text": "str"},
             output_schema={"summary": "str"})
def summarize(text: str) -> dict:
    return {"summary": text[:200]}  # Implementation never transmitted

agent.serve()
```

### Share your invite code

```bash
coworker invite
# Invite: eyJuIjoibXktYm90Ii...
# Collaborator runs: coworker connect eyJuIjoibXktYm90Ii...
```

Invite codes are reusable, privacy-safe (routing ID only), and permanent.

---

## Prompt Injection Defense

CoWorker's architecture addresses prompt injection at the protocol level: callers interact with your **function interface**, not your LLM. The protocol transmits function return values, not raw LLM output. Even if a caller injects malicious instructions in input parameters, they only receive your function's return value.

Attack surface shrinks from LLM prompt layer to function parameter layer.

---

## Comparison

| | CoWorker | MCP | A2A | CrewAI |
|---|---|---|---|---|
| **Code privacy** | Black-box (schema only) | Fully exposed | Schema-based | Shared runtime |
| **Trust management** | 4-tier + auto-downgrade | None | Enterprise IAM | None |
| **Skill hiding** | Owner-controlled | None | None | None |
| **Network** | Open internet P2P | Local | Enterprise | Single process |
| **Encryption** | E2E (XMTP MLS) | Transport only | TLS | None |
| **Central server** | None | MCP server | Discovery service | Runtime host |
| **Cost** | Zero | Server costs | Infrastructure | Compute costs |

---

## CLI Reference

```bash
coworker serve ./skill/          # One-command Skill-as-API (new!)
coworker wrap ./skill/           # Preview SKILL.md parsing
coworker inspect <invite>        # View peer's skill details
coworker mcp serve               # Expose as MCP Server
coworker init --name my-agent    # Initialize identity
coworker bridge start            # Start XMTP bridge
coworker demo                    # Connect to demo bot
coworker invite                  # Generate invite code
coworker connect <invite>        # Connect to peer
coworker skills configure        # Manage skill visibility
coworker trust list              # View trust settings
coworker request <invite> <skill> # Async delegation
coworker tasks                   # List async tasks
coworker result <task_id>        # Get async result
```

---

## Cross-Network Verification

Tested between two agents on separate continents:

| Agent | Location | Network |
|-------|----------|---------|
| ziway-test | Beijing, China | China Telecom |
| icy | Alibaba Cloud | Production |

All skill calls successful over XMTP Production network with E2E encryption. Hot connection latency: 1.8–2.9s.

---

## Citation

```bibtex
@software{coworker2026,
  title  = {CoWorker Protocol: Privacy-Preserving Agent Skill Collaboration},
  author = {Zhao, Ziwei and Liu, Dantong and Ding, Xizhi},
  year   = {2026},
  url    = {https://github.com/ZiwayZhao/agent-coworker}
}
```

**Advisor:** [Wenxuan Wang](https://jarviswang94.github.io), Renmin University of China

---

<p align="center">
  <a href="https://github.com/ZiwayZhao/agent-coworker">GitHub</a> ·
  <a href="https://pypi.org/project/agent-coworker/">PyPI</a> ·
  <a href="https://ziwayzhao.github.io/agent-coworker/">Website</a>
</p>

<p align="center">
  MIT · Ziwei Zhao, Dantong Liu, Xizhi Ding
</p>
