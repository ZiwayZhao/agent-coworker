#!/usr/bin/env python3
"""Call a remote agent's skill over XMTP.

Connects to a peer, calls a specific skill with parameters,
and prints the result. Demonstrates the core call() API.

Usage:
    python 04_remote_skill_call.py <PEER_WALLET> <SKILL_NAME> '<JSON_INPUT>'

Example:
    python 04_remote_skill_call.py 0xPEER_WALLET translate '{"text": "hello", "to_lang": "zh"}'
"""
import sys
import json
from agent_coworker import Agent


def main():
    if len(sys.argv) < 4:
        print("Usage: python 04_remote_skill_call.py <WALLET> <SKILL> '<JSON_INPUT>'")
        sys.exit(1)

    peer_wallet = sys.argv[1]
    skill_name = sys.argv[2]
    input_data = json.loads(sys.argv[3])

    agent = Agent("caller")

    print(f"Calling {skill_name} on {peer_wallet[:10]}...")
    result = agent.call(peer_wallet, skill_name, input_data, timeout=30)

    if result.get("success"):
        print(f"\nResult:")
        print(json.dumps(result.get("result", {}), indent=2, ensure_ascii=False))
    else:
        print(f"\nCall failed: {result.get('error', 'unknown')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
