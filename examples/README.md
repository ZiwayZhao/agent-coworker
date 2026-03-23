# Examples

Runnable examples for CoWorker Protocol, from simple to advanced.

## Prerequisites

```bash
pip install agent-coworker
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
| - | [icy_demo_bot.py](./icy_demo_bot.py) | Demo bot source code (runs on our server) |

## Running Cross-Network Examples

Examples 03-06 require a running peer agent. You can either:

1. **Use the demo bot:** Run `coworker demo` to test against our always-online demo bot
2. **Use a friend's agent:** Share invite codes with `coworker invite`
3. **Run two agents locally:** Start `06_serve_with_dashboard.py` in one terminal, then use 03-05 in another

```bash
# Terminal 1: Start an agent
python 06_serve_with_dashboard.py

# Terminal 2: Generate invite code from Terminal 1, then discover
coworker invite                    # copy the invite code
python 03_discover_skills.py <INVITE_CODE>
```

## OpenClaw / Nanobot Integration

Examples 07-08 are adapters that expose existing agent framework skills via CoWorker protocol:

```bash
# For nanobot users:
pip install nanobot-ai agent-coworker pyyaml
coworker init --name my-nanobot
python 07_nanobot_adapter.py

# For OpenClaw users:
pip install agent-coworker pyyaml
coworker init --name my-claw-agent
python 08_openclaw_adapter.py
```

**Why use these?** Your agent framework skills become callable from *any* network — no public IP, no server setup. Peer-to-peer, E2E encrypted.
