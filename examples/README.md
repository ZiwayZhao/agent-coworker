# Examples

Runnable examples for CoWorker Protocol, from simple to advanced.

## Prerequisites

```bash
pip install coworker-protocol
coworker init --name my-agent
coworker bridge start
```

## Examples

| # | File | What it shows |
|---|------|---------------|
| 01 | [01_minimal.py](./01_minimal.py) | Hello world — register a skill and serve |
| 02 | [02_collaboration.py](./02_collaboration.py) | In-process collaboration via local transport |
| 03 | [03_discover_skills.py](./03_discover_skills.py) | Discover a remote agent's skills via XMTP |
| 04 | [04_remote_skill_call.py](./04_remote_skill_call.py) | Call a remote skill with JSON input |
| 05 | [05_collaborate.py](./05_collaborate.py) | Multi-step goal-based collaboration |
| 06 | [06_serve_with_dashboard.py](./06_serve_with_dashboard.py) | Serve agent with monitoring dashboard |
| 07 | [07_nanobot_adapter.py](./07_nanobot_adapter.py) | Bridge nanobot skills to CoWorker (XMTP) |
| 08 | [08_openclaw_adapter.py](./08_openclaw_adapter.py) | Bridge OpenClaw skills to CoWorker (XMTP) |

## Running Cross-Network Examples

Examples 03-06 require a running peer agent. You can either:

1. **Use a friend's agent:** Share wallet addresses with `coworker invite`
2. **Run two agents locally:** Start `06_serve_with_dashboard.py` in one terminal, then use 03-05 in another

```bash
# Terminal 1: Start an agent
python 06_serve_with_dashboard.py

# Terminal 2: Discover its skills
python 03_discover_skills.py <WALLET_FROM_TERMINAL_1>
```

## OpenClaw / Nanobot Integration

Examples 07-08 are adapters that expose existing agent framework skills via CoWorker protocol:

```bash
# For nanobot users:
pip install nanobot-ai coworker-protocol pyyaml
coworker init --name my-nanobot
python 07_nanobot_adapter.py
# → Auto-discovers ~/.nanobot/workspace/skills/
# → Registers them as CoWorker skills
# → Now accessible to trusted peers over XMTP

# For OpenClaw users:
pip install coworker-protocol pyyaml
coworker init --name my-claw-agent
python 08_openclaw_adapter.py
# → Auto-discovers ~/.openclaw/workspace/skills/
# → Same as above
```

**Why use these?** Your agent framework skills become callable from *any* network — no public IP, no server setup. Just wallet-to-wallet, E2E encrypted.
