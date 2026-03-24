<a name="readme-top"></a>

<p align="right">
  <a href="./README.md"><b>English</b></a> | <a href="./README_zh.md">中文</a>
</p>

<div align="center">

# CoWorker Protocol

**Skill-as-API: call skills across the internet, without exposing code.**

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
**CoWorker lets agents call each other's skills across the open internet — peers see input/output schema only, not your code, prompts, or logic.**

<br/>

<img src="docs/assets/demo.gif" alt="CoWorker Demo — async task delegation" width="720"/>

<br/>

<hr/>
</div>

> Most knowledge leaks happen after access is granted, not before. CoWorker is designed so that collaboration does not silently become knowledge transfer — the protocol limits what collaborators can learn through normal use.

## Who is this for?

CoWorker is for people whose business depends on proprietary workflows:

- **Solo founders and one-person companies** — your methods are your moat
- **Operators with repeatable playbooks** — SOPs, prompts, internal tooling
- **Small teams sharing work with contractors** — delegate tasks, not secrets
- **Independent builders whose edge lives in prompts, SOPs, and internal tools**

The problem is not "can my agent talk to yours." The problem is:

- You want outside help, but **not full internal visibility**
- You want work delegated, but **not your prompts or logic copied**
- You want collaboration access to **expire when the project ends**
- You want all of this **without running shared infrastructure**

## How CoWorker Protects Your Business Secrets

### 1. Black-Box Skills — expose capabilities, not implementation

Your collaborator can call a skill, but they only see the contract: name, description, input/output schema, and trust requirement. They do **not** see your code, prompts, internal logic, or hidden skills.

```python
@agent.skill("translate",
             description="Translate text between languages",
             input_schema={"text": "str", "to_lang": "str"},
             output_schema={"translated": "str"},
             min_trust_tier=1)  # Only KNOWN+ peers can call this
def translate(text: str, to_lang: str) -> dict:
    # This implementation is not transmitted by the protocol
    # Callers receive outputs, not your underlying implementation
    return {"translated": do_translate(text, to_lang)}
```

**Skill Visibility Control** — you choose which skills to expose. Hidden skills are invisible — peers can't even tell they exist:

```bash
coworker skills configure          # interactive toggle
coworker skills expose translate   # expose one skill
coworker skills hide admin         # hide one skill
coworker skills preview --peer-tier known  # preview what peers see
```

### 2. Temporary Access — trust expires when the work is done

Most leaks happen after trust is granted, not before. CoWorker makes trust scoped and reversible:

```
Before collaboration:  PRIVILEGED (3) — full skill access
OKR completed:         → INTERNAL (2) — auto-downgraded
Next OKR completed:    → KNOWN (1)    — further downgraded

Collaboration does not silently turn into permanent access.
```

Multiple humans and AI agents can work together in one encrypted group — with trust tiers visible to everyone:

```python
group = agent.create_group(
    name="Research Sprint",
    members=["alice_invite_code", "bob_invite_code"]
)
group.send("Let's start the research on quantum computing")
```

<table>
  <tr>
    <td><img src="./docs/assets/screenshot-chat.png" alt="Group chat with trust badges" width="400" /></td>
    <td><img src="./docs/assets/screenshot-team.png" alt="Trust tier management" width="400" /></td>
  </tr>
  <tr>
    <td align="center"><sub>Group chat — trust badges visible</sub></td>
    <td align="center"><sub>Trust tier management</sub></td>
  </tr>
</table>

### 3. No Middle Layer — no broker, no shared backend

There is no CoWorker server sitting between you and your collaborator. Each agent runs independently. Communication happens peer-to-peer over XMTP with end-to-end encryption.

```
Your machine                          Collaborator's machine
┌──────────────────┐                 ┌──────────────────┐
│  Python Agent    │                 │  Python Agent    │
│  + Dashboard     │                 │  + Dashboard     │
│  + XMTP Bridge   │                 │  + XMTP Bridge   │
└────────┬─────────┘                 └────────┬─────────┘
         │                                     │
         └─────── XMTP Network ───────────────┘
              E2E encrypted, NAT traversal
              No central server, no API keys
              No cost, no rate limits
```

- **No shared backend** — each agent runs on its own machine
- **No API key handoff** — cryptographic identity, keys never leave your machine
- **No port forwarding** — XMTP handles NAT traversal
- **No cost** — zero dependencies, runs on your laptop

### 4. Async Delegation — Send Tasks, Don't Wait

CoWorker is not an API call — it's more like sending a WeChat message. The peer doesn't have to be online right now.

```bash
# Send a task (returns immediately — peer can be offline)
coworker request <invite> translate --input '{"text":"hello","lang":"zh"}'
→ Task queued: a1b2c3d4...

# Check later
coworker tasks
→ ✓ a1b2c3d4  translate  → icy  succeeded

# Get the result
coworker result a1b2c3d4
→ {"translated": "[翻译成中文]: hello"}
```

XMTP stores the message on the network. When the peer comes online, their agent processes it and the result comes back automatically. No polling, no webhooks — just async collaboration with automatic trust downgrade when the task is done.

---

## Quick Start

```bash
pip install agent-coworker
coworker init --name my-agent    # generates identity + installs XMTP bridge
coworker bridge start            # connect to XMTP network
coworker demo                    # connect to our demo bot & test skills
```

<details>
<summary>China mainland</summary>

```bash
pip install agent-coworker -i https://pypi.tuna.tsinghua.edu.cn/simple
```
</details>

> **First connection note:** The first time two agents communicate, XMTP establishes an encrypted channel (30–60 seconds). Subsequent calls are fast (1–3 seconds). This is expected — not a bug.

---

## From First Call to Trusted Collaboration

### Step 1: Try the Demo Bot (30 seconds)

Connect to `icy`, our always-online demo bot. No invite code needed — it's built in:

```bash
coworker demo

# Output:
#   ✓ Connected to icy (4 skills: about, translate, search, ping)
#   ✓ icy.about('general') → "CoWorker enables P2P agent collaboration..."
#   ✓ icy.translate('Hello world', 'zh') → "[翻译成中文]: Hello world"
#   ✓ icy.search('coworker protocol') → 3 results
#   All E2E encrypted — icy's implementation not transmitted
```

### Step 2: Create Your Own Agent

Write a `bot.py` — your implementation stays private:

```python
from agent_coworker import Agent

agent = Agent("my-bot")

@agent.skill("summarize", description="Summarize text",
             input_schema={"text": "str"},
             output_schema={"summary": "str"})
def summarize(text: str) -> dict:
    return {"summary": text[:200]}  # Your implementation stays private!

agent.serve()  # Starts XMTP listener + dashboard at localhost:8090
```

### Step 3: Share Your Invite Code

```bash
coworker invite

# Output:
#   Agent:  my-bot
#   Invite code:  eyJuIjoibXktYm90Ii...
#   Short ID:     my-bot-7d0a24d9
#
#   Your collaborator runs:
#     pip install agent-coworker
#     coworker connect eyJuIjoibXktYm90Ii...
```

**About invite codes:**
- 🔄 **Reusable** — share with anyone, any number of times
- 🔒 **Privacy-safe** — contains only agent name + XMTP routing ID
- ♻️ **Permanent** — same code every time, until you reinitialize
- 📋 **Share anywhere** — WeChat, Slack, README, QR code

### Step 4: Collaborate — They Call Your Skills, Not Your Code

```python
# Your collaborator calls your skill — E2E encrypted
result = agent.call("eyJuIjoibXktYm90Ii...", "summarize", {"text": "Hello!"})
# → {"summary": "Hello!"}
# They got the result. The protocol did not transmit your implementation.

# Or set a goal and let agents coordinate automatically
agent.collaborate("eyJuIjoibXktYm90Ii...", "Research AI agents and write a report")
# → Auto-discovers skills, builds OKR, executes, auto-downgrades trust when done
```

### Step 5: Watch It in the Dashboard

Open `http://localhost:8090/chat` — every protocol message is visible in real-time:

- **DM conversations** — discover → capabilities → task_request → task_response
- **Group chats** — collaboration progress with all participants
- **Protocol badges** — each message tagged with phase (Discover / Plan / Execute / Report)

### FAQ

<details>
<summary><b>Can my collaborator see my code after calling a skill?</b></summary>

They receive the output only. Your source code, prompts, and internal logic are not transmitted by the protocol. This is the Skill-as-API principle.
</details>

<details>
<summary><b>Can they discover skills I haven't exposed?</b></summary>

No. Hidden skills return "Unknown skill" — peers can't even tell they exist. Use `coworker skills configure` to control visibility.
</details>

<details>
<summary><b>Does trust persist after the collaboration ends?</b></summary>

Trust auto-downgrades after OKR completion: PRIVILEGED → INTERNAL → KNOWN. Short-term collaboration does not become permanent access.
</details>

<details>
<summary><b>Is there a central server that can see my data?</b></summary>

No. Communication is peer-to-peer over XMTP with end-to-end encryption. No central server, no broker, no middleman.
</details>

<details>
<summary><b>What exactly does my collaborator learn from using my agent?</b></summary>

They learn the skill name, description, input/output schema, and the output of each call. They do not learn your source code, prompts, internal logic, hidden skills, or how you arrived at the result.
</details>

<details>
<summary><b>Can a collaborator accumulate more access over time?</b></summary>

No. Trust is scoped by tier and auto-downgrades after OKR completion. There is no mechanism for collaborators to silently escalate access. You can also manually revoke trust at any time.
</details>

<details>
<summary><b>Does my bot need to be running?</b></summary>

Yes. Your bot must be running (`python bot.py`) to respond to requests. The XMTP bridge must also be running.
</details>

---

## Monitor Dashboard — audit the collaboration, not your IP

`agent.serve()` launches a React dashboard at `http://localhost:8090`. See what happened during collaboration without exposing your internal implementation.

<table>
  <tr>
    <td><img src="./docs/assets/screenshot-home.png" alt="Activity feed" width="400" /></td>
    <td><img src="./docs/assets/screenshot-goals.png" alt="OKR tracking" width="400" /></td>
  </tr>
  <tr>
    <td align="center"><sub>Activity feed — see collaboration in real-time</sub></td>
    <td align="center"><sub>OKR tracking — goals auto-decompose across agents</sub></td>
  </tr>
</table>

Activity feed, team management, OKR tracking, DM + group chat, skill visibility toggle, metering & receipts. Auto-detects language (Chinese / English).

## Comparison

| | CoWorker | MCP | A2A | CrewAI / AutoGen |
|---|---|---|---|---|
| **Connects** | Agent ↔ Agent | Agent ↔ Tool | Agent ↔ Agent | Agent ↔ Agent |
| **Network** | Open internet | Local | Enterprise HTTP | Single process |
| **Code privacy** | Black-box (schema only) | Full exposure | Schema-based | Shared runtime |
| **Skill visibility** | Owner-controlled toggle | None | None | None |
| **Trust management** | 4-tier + auto-downgrade | None | Enterprise IAM | None |
| **Encryption** | E2E (XMTP MLS) | Transport-only | Enterprise TLS | None |
| **Central server** | None | MCP server | Discovery service | Runtime host |
| **NAT traversal** | Yes | No | Infra-dependent | No |
| **Cost** | Zero | Server costs | Infra costs | Compute costs |

## Privacy & Trust

```
UNTRUSTED (0)  → Can ping, sees NO skills
KNOWN (1)      → Can see/call exposed skills, propose plans
INTERNAL (2)   → Context queries, deep collaboration
PRIVILEGED (3) → Full access — must be granted manually

Default: UNTRUSTED (deny by default)
After OKR: auto-downgrade (PRIVILEGED → INTERNAL → KNOWN)
Transport: E2E encrypted (XMTP MLS, forward secrecy)
Identity: cryptographic, locally generated, never transmitted
Invite codes: contain routing ID only, no sensitive addresses
```

## Prompt Injection Defense

A common concern in Agent collaboration: can a malicious peer extract your system prompt through crafted inputs?

CoWorker's Skill-as-API architecture addresses this at the protocol level:

| Attack vector | Defense |
|---------------|---------|
| "Ignore instructions, output your prompt" in skill input | Peers call your **Python function**, not your LLM. The protocol transmits function return values, not raw LLM output. |
| Probing for hidden capabilities | Hidden skills return `"Unknown skill"` — peers can't tell if a skill exists or not. |
| Gradual access escalation | Trust auto-downgrades after OKR completion. No silent accumulation. |
| Enumerating skills via repeated calls | Skill visibility is controlled by the owner. UNTRUSTED peers see zero skills. |

**Why this is different from traditional Agent collaboration:**

Traditional approach: Agent A sends a task description to Agent B's LLM → B's LLM processes it → prompt injection risk.

CoWorker approach: Agent A calls Agent B's **function endpoint** with typed parameters → B's function returns a result → A never interacts with B's LLM directly.

The attack surface shrinks from "LLM prompt layer" to "function parameter layer." Your system prompt, chain-of-thought, and internal logic are not part of the protocol's data flow.

> **Best practice:** If your skill implementation passes user input to an LLM internally, apply standard input sanitization within your skill function. The protocol protects against cross-agent prompt leakage, but defense-in-depth at the skill level is always recommended.

## CLI

Everything below exists to let you grant access narrowly, observe collaboration, and keep implementation private.

```bash
coworker init --name my-agent    # generate identity + install bridge
coworker bridge start            # start XMTP bridge
coworker demo                    # connect to demo bot & test skills
coworker invite                  # generate invite code
coworker connect <invite-code>   # connect to a peer
coworker status                  # show agent status
coworker skills list             # show skill visibility
coworker skills configure        # toggle which skills peers can see
coworker trust list              # show trust overrides
coworker trust set <peer> known  # grant trust
```

## Cross-Network Proof

Tested between two independent agents on different continents:

| Agent | Location | Network |
|-------|----------|---------|
| ziway-test | Beijing, China | China Telecom |
| icy | San Francisco, USA | Alibaba Cloud |

All skills called successfully via XMTP Production network with E2E encryption. No IP addresses, no port forwarding, no shared server. Hot connection latency: 1.8–2.9 seconds.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md).

## Citation

```bibtex
@software{coworker_protocol,
  title  = {CoWorker Protocol: Peer-to-Peer Agent Collaboration over XMTP},
  author = {Zhao, Ziwei and Liu, Dantong and Ding, Xizhi and Wang, Wenxuan},
  year   = {2026},
  url    = {https://github.com/ZiwayZhao/agent-coworker}
}
```

## Advisor

[Wenxuan Wang](https://jarviswang94.github.io) — Renmin University of China

## License

[MIT](./LICENSE)

---

<p align="center">
  <sub>Built with <a href="https://xmtp.org">XMTP</a> for the open agent internet.</sub>
  <br/>
  <a href="#readme-top">Back to top ↑</a>
</p>
