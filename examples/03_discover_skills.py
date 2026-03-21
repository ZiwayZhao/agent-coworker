#!/usr/bin/env python3
"""Discover a remote agent's skills via XMTP.

Shows how to connect to a peer by wallet address, list their skills,
and inspect skill schemas before calling.

Usage:
    python 03_discover_skills.py <PEER_WALLET_ADDRESS>
"""
import sys
from agent_coworker import Agent

def main():
    if len(sys.argv) < 2:
        print("Usage: python 03_discover_skills.py <PEER_WALLET_ADDRESS>")
        print("Example: python 03_discover_skills.py 0xPEER_WALLET_ADDRESS")
        sys.exit(1)

    peer_wallet = sys.argv[1]
    agent = Agent("explorer")

    print(f"Discovering skills on {peer_wallet}...")
    peer = agent.connect(peer_wallet)

    if "error" in peer:
        print(f"Failed to connect: {peer['error']}")
        sys.exit(1)

    print(f"\nPeer: {peer.get('name', 'unknown')}")
    print(f"Skills found: {len(peer.get('skills', []))}\n")

    for skill in peer.get("skills", []):
        if isinstance(skill, dict):
            print(f"  {skill['name']}: {skill.get('description', '')}")
            if skill.get("input_schema"):
                print(f"    Input:  {skill['input_schema']}")
            if skill.get("output_schema"):
                print(f"    Output: {skill['output_schema']}")
            print()
        else:
            print(f"  {skill}")


if __name__ == "__main__":
    main()
