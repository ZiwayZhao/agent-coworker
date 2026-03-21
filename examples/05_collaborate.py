#!/usr/bin/env python3
"""Multi-step collaboration between two agents.

Sends a high-level goal to a peer agent and lets the CoWorker protocol
plan and execute the task using both agents' skills.

Usage:
    python 05_collaborate.py <PEER_WALLET> "Research AI and write a report"
"""
import sys
from agent_coworker import Agent


def main():
    if len(sys.argv) < 3:
        print('Usage: python 05_collaborate.py <WALLET> "goal description"')
        sys.exit(1)

    peer_wallet = sys.argv[1]
    goal = sys.argv[2]

    # Create an agent with local skills
    agent = Agent("researcher")

    @agent.skill("analyze", description="Analyze data",
                 input_schema={"data": "str", "goal": "str"},
                 output_schema={"insights": "list"})
    def analyze(data: str = "", goal: str = "") -> dict:
        return {"insights": [f"Key finding about {data or goal}"]}

    @agent.skill("draft", description="Draft a document",
                 input_schema={"topic": "str"},
                 output_schema={"content": "str"})
    def draft(topic: str = "") -> dict:
        return {"content": f"Report on {topic}"}

    print(f"Starting collaboration: {goal}")
    print(f"Peer: {peer_wallet[:10]}...\n")

    result = agent.collaborate(peer_wallet, goal)

    if result.get("success"):
        steps = result.get("steps", [])
        completed = [s for s in steps if s.get("status") == "completed"]
        print(f"Collaboration complete: {len(completed)}/{len(steps)} steps done")
        for step in steps:
            status = step.get("status", "?")
            skill = step.get("skill", "?")
            marker = "+" if status == "completed" else "x"
            print(f"  [{marker}] {skill}: {status}")
    else:
        print(f"Collaboration result: {result.get('error', 'see details')}")


if __name__ == "__main__":
    main()
