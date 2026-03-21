"""Minimal coworker agent example."""

from agent_coworker import Agent

agent = Agent("my-bot")


@agent.skill("echo", description="Echo input back")
def echo(text: str) -> dict:
    return {"output": text}


if __name__ == "__main__":
    print("Agent ready. Run agent.serve() to start daemon.")
    agent.serve()
