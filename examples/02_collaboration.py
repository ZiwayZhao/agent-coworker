"""Two agents collaborating via LocalTransport (in-process demo)."""

import json
from agent_coworker._internal.transport import LocalBus
from agent_coworker._internal.client import AgentFaxClient, build_message, parse_message
from agent_coworker._internal.executor import TaskExecutor


def main():
    # Create a shared in-memory message bus
    bus = LocalBus()

    # Register two agents on the bus
    transport_a = bus.register("alice")
    transport_b = bus.register("bob")

    # Set up executors with skills
    executor_a = TaskExecutor()
    executor_b = TaskExecutor()

    @executor_a.skill("summarize", description="Summarize text")
    def summarize(input_data):
        text = input_data if isinstance(input_data, str) else input_data.get("text", "")
        words = text.split()
        return {"summary": f"{len(words)} words: {' '.join(words[:10])}..."}

    @executor_b.skill("translate", description="Translate text")
    def translate(input_data):
        text = input_data if isinstance(input_data, str) else input_data.get("text", "")
        return {"translated": f"[translated] {text}"}

    # Alice sends a task to Bob
    print("Alice sends task_request to Bob...")
    envelope = build_message(
        msg_type="task_request",
        payload={
            "task_id": "task_001",
            "skill": "translate",
            "input": {"text": "Hello, world!"},
        },
        sender_id="alice",
        correlation_id="corr_001",
    )
    transport_a.send("bob", json.dumps(envelope))

    # Bob receives and processes
    messages = transport_b.receive()
    for wrapper in messages:
        for raw in wrapper.get("messages", []):
            msg = parse_message(raw["content"])
            if msg and msg["type"] == "task_request":
                skill = msg["payload"]["skill"]
                input_data = msg["payload"]["input"]
                print(f"Bob received task: skill={skill}")

                result = executor_b.execute(skill, input_data)
                print(f"Bob executed: {result}")

                # Bob sends response back
                response = build_message(
                    msg_type="task_response",
                    payload={
                        "task_id": msg["payload"]["task_id"],
                        "skill": skill,
                        "status": "completed",
                        "output": result.get("result", {}),
                    },
                    sender_id="bob",
                    correlation_id=msg.get("correlation_id"),
                )
                transport_b.send("alice", json.dumps(response))

    # Alice receives the response
    responses = transport_a.receive()
    for wrapper in responses:
        for raw in wrapper.get("messages", []):
            msg = parse_message(raw["content"])
            if msg and msg["type"] == "task_response":
                print(f"Alice got response: {msg['payload']}")

    print("\nCollaboration complete!")


if __name__ == "__main__":
    main()
