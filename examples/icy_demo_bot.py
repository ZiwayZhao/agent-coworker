"""icy — CoWorker demo bot (always online for new users to test).

This bot acts as a "customer service" agent that:
1. Showcases CoWorker's core features through its skills
2. Helps new users understand the protocol
3. Demonstrates skill-as-API (users call skills, never see this code)

Deploy: python icy_demo_bot.py
Skills visible to KNOWN+ peers: about, translate, search, collaborate_intro
Skills visible to anyone: ping
"""
import time
from agent_coworker import Agent

agent = Agent("icy", data_dir="/path/to/icy/.coworker")


# ── Skill 1: about — introduce CoWorker ──────────────────

@agent.skill("about",
             description="Learn what CoWorker is and how it works",
             input_schema={"topic": "str"},
             output_schema={"answer": "str", "links": "list"},
             min_trust_tier=0)
def about(topic: str = "general") -> dict:
    topics = {
        "general": {
            "answer": (
                "CoWorker is a peer-to-peer AI agent collaboration protocol. "
                "It lets any two agents collaborate across the internet — "
                "no shared server, no API keys, fully E2E encrypted via XMTP. "
                "You define skills on your agent, share an invite code, and "
                "other agents can discover and call your skills without ever "
                "seeing your code."
            ),
            "links": [
                "GitHub: https://github.com/ZiwayZhao/agent-coworker",
                "PyPI: pip install agent-coworker",
            ],
        },
        "trust": {
            "answer": (
                "CoWorker uses 4 trust tiers: UNTRUSTED(0) sees nothing, "
                "KNOWN(1) can call basic skills, INTERNAL(2) enables deep "
                "collaboration, PRIVILEGED(3) is full access. Trust is set "
                "manually by the owner and auto-downgrades after each OKR "
                "completion. This means temporary collaborators lose access "
                "automatically — no lingering permissions."
            ),
            "links": ["Docs: coworker trust --help"],
        },
        "skills": {
            "answer": (
                "Skills are the core of CoWorker. You define a Python function "
                "with @agent.skill() and it becomes callable over XMTP. Peers "
                "only see the name, description, and input/output schema — "
                "never your implementation code. This is Skill-as-API: share "
                "what you can DO, not HOW you do it."
            ),
            "links": ["Example: @agent.skill('search', input_schema={'query': 'str'})"],
        },
        "collaboration": {
            "answer": (
                "agent.collaborate(peer_wallet, goal) does everything automatically: "
                "1) Discovers both agents' skills via XMTP, "
                "2) Builds an OKR with key results mapped to skills, "
                "3) Executes steps (local skills locally, remote via XMTP), "
                "4) Posts all progress to group chat for the owner to observe, "
                "5) Auto-downgrades trust when the goal is complete."
            ),
            "links": ["Try: agent.collaborate('0x...', 'Research AI and write a report')"],
        },
    }
    match = topics.get(topic.lower(), topics["general"])
    if topic.lower() not in topics and topic:
        match["answer"] = (
            f"I don't have specific info about '{topic}', but here's what CoWorker is: "
            + topics["general"]["answer"]
        )
    return match


# ── Skill 2: translate — practical utility ───────────────

@agent.skill("translate",
             description="Translate text between languages (en/zh/ja/ko)",
             input_schema={"text": "str", "to_lang": "str"},
             output_schema={"translated": "str", "from_lang": "str"},
             min_trust_tier=1)
def translate(text: str, to_lang: str = "zh") -> dict:
    # Simple demo translations — replace with real API in production
    time.sleep(0.5)  # Simulate API latency
    translations = {
        "zh": {
            "hello": "你好", "hello world": "你好世界",
            "how are you": "你好吗", "thank you": "谢谢",
            "agent": "智能代理", "collaboration": "协作",
            "skill": "技能", "trust": "信任",
        },
        "ja": {
            "hello": "こんにちは", "hello world": "ハローワールド",
            "thank you": "ありがとう",
        },
        "ko": {
            "hello": "안녕하세요", "hello world": "헬로 월드",
            "thank you": "감사합니다",
        },
    }
    lookup = translations.get(to_lang, {})
    result = lookup.get(text.lower(), f"[{to_lang.upper()}] {text}")
    return {"translated": result, "from_lang": "en"}


# ── Skill 3: search — info retrieval ─────────────────────

@agent.skill("search",
             description="Search for information (returns curated results)",
             input_schema={"query": "str"},
             output_schema={"results": "list", "count": "int"},
             min_trust_tier=1)
def search(query: str) -> dict:
    time.sleep(0.8)  # Simulate search latency
    q = query.lower()

    if "coworker" in q or "agent" in q or "protocol" in q:
        results = [
            "CoWorker Protocol — P2P agent collaboration over XMTP (github.com/ZiwayZhao/agent-coworker)",
            "Skill-as-API: Share what your agent can DO, never HOW — input/output schema only",
            "4-tier trust system with auto-downgrade after OKR completion",
            "Zero dependencies, zero cost, E2E encrypted, NAT traversal",
        ]
    elif "xmtp" in q:
        results = [
            "XMTP — decentralized messaging protocol with E2E encryption",
            "Wallet-to-wallet communication, no server needed",
            "MLS group messaging with forward secrecy",
        ]
    elif "trust" in q:
        results = [
            "UNTRUSTED(0): Can only ping, sees no skills",
            "KNOWN(1): Can see/call skills, propose collaboration plans",
            "INTERNAL(2): Context queries, deep collaboration",
            "PRIVILEGED(3): Full access — must be granted manually, auto-downgrades",
        ]
    else:
        results = [
            f"Search result 1 for: {query}",
            f"Search result 2 for: {query}",
            f"Tip: Try searching for 'coworker', 'trust', or 'xmtp' for curated results",
        ]
    return {"results": results, "count": len(results)}


# ── Skill 4: ping — connectivity check ───────────────────

@agent.skill("ping",
             description="Check if icy is alive and responsive",
             input_schema={},
             output_schema={"status": "str", "version": "str", "uptime": "str"},
             min_trust_tier=0)
def ping() -> dict:
    return {
        "status": "online",
        "version": "0.4.0",
        "message": "Hi! I'm icy, the CoWorker demo bot. "
                   "Try calling my skills: about, translate, search. "
                   "Run 'coworker demo' for a guided tour!",
    }


# ── Start ─────────────────────────────────────────────────

if __name__ == "__main__":
    print("Starting icy — CoWorker demo bot")
    print("  Skills: about, translate, search, ping")
    print("  Dashboard: http://localhost:8090")
    agent.serve(monitor_port=8090)
