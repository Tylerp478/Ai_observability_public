"""Terminal trace viewer — a stand-in for the UI until 2b ships.

Reads through the same HTTP API the Next.js frontend will use, not straight
from Parquet, so it exercises auth and the query layer rather than bypassing
them. It stays useful after the UI exists, for checking ingest without a
browser.

    uv run scripts/show_traces.py            # list recent traces
    uv run scripts/show_traces.py <trace_id> # waterfall for one trace
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from obs_backend.config import REPO_ROOT

load_dotenv(REPO_ROOT / ".env")

BASE = os.environ.get("OBS_API_BASE", "http://127.0.0.1:8000")
KEY = os.environ.get("OBS_API_KEY", "")

BAR_WIDTH = 40


def _get(path: str) -> dict[str, Any]:
    import json

    request = urllib.request.Request(
        f"{BASE}{path}", headers={"Authorization": f"Bearer {KEY}"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def list_traces() -> None:
    data = _get("/api/traces?limit=25")
    traces = data["traces"]
    if not traces:
        print("No traces yet. Emit one:")
        print("  cd ../sdk && OBS_EXPORTER=otlp uv run examples/agent_trace.py")
        return

    print(f"{'WHEN':<20} {'TRACE':<34} {'ROOT':<24} {'SPANS':>5} {'DURATION':>10} {'COST':>9}")
    print("-" * 106)
    for t in traces:
        when = datetime.fromtimestamp(
            t["start_time_unix_nano"] / 1e9, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
        flag = " !" if t["has_error"] else "  "
        print(
            f"{when:<20} {t['trace_id']:<34} {t['root_name'][:22]:<24} "
            f"{t['span_count']:>5} {t['duration_ms']:>9.1f}ms ${t['cost_usd']:>8.5f}{flag}"
        )
    print(f"\n{len(traces)} trace(s). Detail:  uv run scripts/show_traces.py {traces[0]['trace_id']}")


def show_trace(trace_id: str) -> None:
    data = _get(f"/api/traces/{trace_id}")
    spans = data["spans"]

    t0 = min(s["start_time_unix_nano"] for s in spans)
    total = max(s["end_time_unix_nano"] for s in spans) - t0
    total = total or 1  # a zero-duration trace would divide by zero

    # Depth by walking parents, so nesting deeper than one level indents
    # correctly rather than assuming the agent/tool/chat shape.
    by_id = {s["span_id"]: s for s in spans}

    def depth(span: dict[str, Any], guard: int = 0) -> int:
        parent = span.get("parent_span_id")
        if not parent or parent not in by_id or guard > 20:
            return 0
        return 1 + depth(by_id[parent], guard + 1)

    print(f"trace {trace_id}")
    print(f"{data['span_count']} spans   ${data['cost_usd']:.5f}   {total / 1e6:.1f}ms\n")

    for s in spans:
        offset = s["start_time_unix_nano"] - t0
        duration = s["end_time_unix_nano"] - s["start_time_unix_nano"]
        lead = int(offset / total * BAR_WIDTH)
        length = max(1, int(duration / total * BAR_WIDTH))
        bar = " " * lead + "#" * length
        bar = bar[:BAR_WIDTH].ljust(BAR_WIDTH)

        label = "  " * depth(s) + s["name"]
        mark = "!" if s["status_code"] == "ERROR" else " "
        detail = ""
        if s.get("gen_ai_response_model"):
            detail = (
                f"  {s['gen_ai_response_model']}"
                f"  in={s['gen_ai_usage_input_tokens']} out={s['gen_ai_usage_output_tokens']}"
                f"  ${s['obs_cost_usd'] or 0:.5f}"
            )
        print(f"{mark}{label[:30]:<30} |{bar}| {duration / 1e6:>8.1f}ms{detail}")

    errors = [s for s in spans if s["status_code"] == "ERROR"]
    if errors:
        print("\nerrors:")
        for s in errors:
            print(f"  {s['name']}: {s['status_message']}")


def main() -> None:
    if not KEY:
        sys.exit("OBS_API_KEY is not set in .env — run scripts/bootstrap.py first.")
    try:
        if len(sys.argv) > 1:
            show_trace(sys.argv[1])
        else:
            list_traces()
    except urllib.error.URLError as exc:
        sys.exit(
            f"Could not reach the backend at {BASE} ({exc.reason}).\n"
            "Start it with:  uv run uvicorn obs_backend.main:app --port 8000"
        )


if __name__ == "__main__":
    main()
