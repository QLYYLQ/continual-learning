#!/usr/bin/env python3
"""
Continuous Learning v4 - Stage 2a Pre-processor

For each new session, generates a lightweight summary file for the
first LLM call (intra-session task segmentation).

Input:  _index.json + .stage2_cursor.json → find new sessions
Output: cache/stage2a/{sid}.json per session + cache/stage2a_manifest.json

Exit codes:
  0 — new sessions found, manifest written
  2 — no new sessions to process
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def load_json(path: str, default: dict | None = None) -> dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def parse_iso(ts_str: str) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def build_session_summary(session: dict) -> dict:
    """Build a lightweight summary for Stage 2a LLM consumption."""
    turns = session.get("turns", [])
    summary_turns = []
    for turn in turns:
        tools = turn.get("tools", {})
        tool_summary = ", ".join(f"{k}:{v}" for k, v in sorted(tools.items()))
        t = {
            "turn_idx": turn.get("turn_idx", 0),
            "prompt": turn.get("prompt", ""),
            "cwd": turn.get("cwd", ""),
            "tool_summary": tool_summary,
            "bash_commands": turn.get("bash_commands", []),
            "fail_count": turn.get("fail_count", 0),
            "delegates": [d.get("agent", "?") for d in turn.get("delegates", [])],
            "duration_ms": turn.get("duration_ms", 0),
        }
        # Include subagent info if present
        if turn.get("subagent_starts"):
            t["subagent_starts"] = turn["subagent_starts"]
        if turn.get("subagent_stops"):
            t["subagent_stops"] = turn["subagent_stops"]
        summary_turns.append(t)

    return {
        "sid": session.get("session_id", ""),
        "start": session.get("time_range", {}).get("start", ""),
        "end": session.get("time_range", {}).get("end", ""),
        "primary_cwd": session.get("primary_cwd", ""),
        "turn_count": session.get("turn_count", 0),
        "signals": session.get("signals", {}),
        "turns": summary_turns,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="CL v4 Stage 2a Pre-processor")
    parser.add_argument("--index", required=True, help="Path to _index.json")
    parser.add_argument("--cursor", required=True, help="Path to .stage2_cursor.json")
    parser.add_argument("--sessions-dir", required=True, help="Path to sessions directory")
    parser.add_argument("--output-dir", required=True, help="Output directory for session summaries")
    parser.add_argument("--manifest", required=True, help="Output manifest path")
    parser.add_argument("--start-time", default=None, help="Filter: sessions starting at or after")
    parser.add_argument("--end-time", default=None, help="Filter: sessions starting at or before")
    args = parser.parse_args()

    index = load_json(args.index)
    cursor = load_json(args.cursor, {"processed_sessions": {}})
    processed = cursor.get("processed_sessions", {})
    sessions = index.get("sessions", [])

    start_filter = parse_iso(args.start_time) if args.start_time else None
    end_filter = parse_iso(args.end_time) if args.end_time else None

    # Find new/changed sessions
    new_session_entries = []
    for entry in sessions:
        sid = entry["sid"]
        event_count = entry.get("event_count", 0)

        prev = processed.get(sid)
        if prev is not None and event_count <= prev.get("event_count", 0):
            continue

        if start_filter or end_filter:
            session_start = parse_iso(entry.get("start", ""))
            if session_start:
                if start_filter and session_start < start_filter:
                    continue
                if end_filter and session_start > end_filter:
                    continue
            else:
                continue

        new_session_entries.append(entry)

    if not new_session_entries:
        print("new_count=0")
        sys.exit(2)

    # Generate per-session summary files
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_sessions = []
    for entry in new_session_entries:
        sid = entry["sid"]
        session_path = Path(args.sessions_dir) / f"{sid}.json"
        session = load_json(str(session_path))
        if not session:
            continue

        summary = build_session_summary(session)
        summary_path = output_dir / f"{sid}.json"
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        manifest_sessions.append({
            "sid": sid,
            "path": str(summary_path),
            "primary_cwd": entry.get("primary_cwd", ""),
            "start": entry.get("start", ""),
            "turn_count": entry.get("turn_count", 0),
        })

    manifest = {"new_sessions": manifest_sessions}
    with open(args.manifest, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"new_count={len(manifest_sessions)}")


if __name__ == "__main__":
    main()
