"""Demo: one real Anthropic call, wrapped in an OTel span.

Run with:
    uv run examples/basic_call.py

Requires ANTHROPIC_API_KEY set (in your environment or a .env file at the
project root — see .env.example).
"""

from anthropic import Anthropic
from dotenv import load_dotenv

from obs_sdk import traced_completion

load_dotenv()


def main() -> None:
    client = Anthropic()

    response = traced_completion(
        client,
        model="claude-opus-5",
        # Opus 5 runs adaptive thinking by default, and max_tokens caps
        # thinking + response text together. A budget sized for the visible
        # answer alone (256 was fine on Sonnet 4.5) can be consumed by thinking
        # and truncate the reply — check finish_reasons if output looks empty.
        max_tokens=4096,
        messages=[{"role": "user", "content": "In one sentence, what is OpenTelemetry?"}],
    )

    print("\n--- response text ---")
    for block in response.content:
        if block.type == "text":
            print(block.text)


if __name__ == "__main__":
    main()
