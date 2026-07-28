"""Demo: a nested agent trace — the shape the waterfall view is built for.

Produces one trace with four spans:

    agent answer_question          (root)
    ├── tool search_docs           (fake retrieval, no API cost)
    ├── chat claude-opus-5         (real, paid)
    └── tool format_citation       (fake, no API cost)

Run with:
    uv run examples/agent_trace.py

Console output (default):
    OBS_EXPORTER=console uv run examples/agent_trace.py

Send to the backend instead (requires OBS_API_KEY):
    OBS_EXPORTER=otlp uv run examples/agent_trace.py
"""

import time

from anthropic import Anthropic
from dotenv import load_dotenv

from obs_sdk import agent_step, shutdown, tool_call, traced_completion

load_dotenv()

# Stand-in for a real retrieval corpus. The point of this example is span
# structure, not retrieval quality — keeping it fake means re-running costs
# only the one LLM call.
FAKE_DOCS = {
    "otel": "OpenTelemetry is a CNCF observability framework for traces, metrics, and logs.",
    "genai": "The gen_ai.* semantic conventions describe spans for LLM and agent operations.",
}


def search_docs(query: str) -> str:
    with tool_call("search_docs", **{"obs.tool.query": query}) as span:
        time.sleep(0.15)  # stand in for I/O so the waterfall has visible width
        hits = [text for key, text in FAKE_DOCS.items() if key in query.lower()]
        span.set_attribute("obs.tool.result_count", len(hits))
        return "\n".join(hits) if hits else "No results."


def format_citation(source: str) -> str:
    with tool_call("format_citation", **{"obs.tool.source": source}):
        time.sleep(0.05)
        return f"[source: {source}]"


def main() -> None:
    client = Anthropic()
    question = "What is OpenTelemetry and what are gen_ai conventions?"

    try:
        with agent_step("answer_question", **{"obs.user.question": question}) as root:
            context = search_docs("otel genai")

            response = traced_completion(
                client,
                model="claude-opus-5",
                max_tokens=4096,
                messages=[
                    {
                        "role": "user",
                        "content": f"Using only this context:\n{context}\n\n"
                        f"Answer in two sentences: {question}",
                    }
                ],
            )

            citation = format_citation("internal-docs")
            root.set_attribute("obs.answer.has_citation", bool(citation))

        print("\n--- response text ---")
        for block in response.content:
            if block.type == "text":
                print(block.text)
        print(citation)
    finally:
        # BatchSpanProcessor buffers in memory; without this a short script can
        # exit before anything is exported.
        shutdown()


if __name__ == "__main__":
    main()
