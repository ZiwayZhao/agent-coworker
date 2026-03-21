#!/usr/bin/env python3
"""Start an agent with the monitoring dashboard.

Registers multiple skills and starts the XMTP listener + HTTP dashboard.
Open http://localhost:8090 to see the live monitoring UI.

Usage:
    python 06_serve_with_dashboard.py
"""
from agent_coworker import Agent

agent = Agent("demo-agent")


@agent.skill("translate", description="Translate text between languages",
             input_schema={"text": "str", "to_lang": "str"},
             output_schema={"translated": "str"})
def translate(text: str = "", to_lang: str = "en") -> dict:
    translations = {"zh": "你好世界", "es": "hola mundo", "fr": "bonjour le monde"}
    return {"translated": translations.get(to_lang, text)}


@agent.skill("summarize", description="Summarize text to N words",
             input_schema={"text": "str", "max_words": "int"},
             output_schema={"summary": "str", "word_count": "int"})
def summarize(text: str = "", max_words: int = 10) -> dict:
    words = text.split()[:max_words]
    return {"summary": " ".join(words), "word_count": len(words)}


@agent.skill("search", description="Search for information",
             input_schema={"query": "str"},
             output_schema={"results": "list"})
def search(query: str = "") -> dict:
    return {"results": [f"Result 1 for '{query}'", f"Result 2 for '{query}'"]}


if __name__ == "__main__":
    print("Starting agent with 3 skills...")
    print("Dashboard: http://localhost:8090")
    print("Press Ctrl+C to stop.\n")
    agent.serve(monitor_port=8090)
