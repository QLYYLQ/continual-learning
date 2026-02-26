#!/usr/bin/env python3
"""
Continuous Learning v3 - Episode Builder (Stage 1)

Pure Python, no LLM. Converts raw JSONL events into structured Turn lists
grouped by session.

Input:  data/turns.jsonl
Output: data/episodes.json

Does NOT perform Episode grouping (that's Stage 2's LLM job).
Only structures raw events into Turns with statistics.
"""

import json
import sys
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CL_DIR = Path.home() / ".claude" / "continual-learning"
DEFAULT_INPUT = CL_DIR / "data" / "turns.jsonl"
DEFAULT_OUTPUT = CL_DIR / "data" / "episodes.json"


def parse_jsonl(path: str) -> list[dict]:
    """Read JSONL file, skip malformed lines."""
    events = []
    with open(path, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
                if evt.get("v") == 3:
                    events.append(evt)
            except json.JSONDecodeError:
                print(f"Warning: skipping malformed line {line_num}", file=sys.stderr)
    return events


def group_by_session(events: list[dict]) -> dict[str, list[dict]]:
    """Group events by session_id, preserving order."""
    sessions: dict[str, list[dict]] = defaultdict(list)
    for evt in events:
        sid = evt.get("sid", "unknown")
        sessions[sid].append(evt)
    return dict(sessions)


def build_turns(session_events: list[dict]) -> list[dict]:
    """
    Split a session's events into Turns.

    A Turn starts with a 'turn' event (user prompt) and includes all
    subsequent tool/delegate/skill/bash_ok/fail/stop events until the
    next 'turn' event.
    """
    turns: list[dict] = []
    current_turn: dict[str, Any] | None = None

    def finalize_turn(turn: dict) -> dict:
        """Compute statistics for a completed Turn."""
        events = turn.pop("_events", [])

        # Tool counts
        tool_counts: dict[str, int] = defaultdict(int)
        fail_count = 0
        delegates: list[dict] = []
        bash_results: list[dict] = []
        grep_patterns: list[str] = []
        files_touched: set[str] = set()
        skills_used: list[str] = []

        for evt in events:
            e_type = evt.get("e")

            if e_type == "tool":
                tool_name = evt.get("tool", "?")
                tool_counts[tool_name] += 1

                # Track files
                target = evt.get("target", "")
                if target and ("/" in target or "." in target):
                    files_touched.add(target)

                # Track grep patterns
                if tool_name == "Grep":
                    pattern = evt.get("pattern", "")
                    if pattern:
                        grep_patterns.append(pattern)

            elif e_type == "delegate":
                delegates.append({
                    "agent": evt.get("agent", "?"),
                    "prompt_preview": evt.get("agent_prompt", "")[:200]
                })
                tool_counts["Task"] += 1

            elif e_type == "skill":
                skills_used.append(evt.get("skill", "?"))
                tool_counts["Skill"] += 1

            elif e_type == "bash_ok":
                bash_results.append({
                    "cmd": evt.get("cmd", ""),
                    "out_preview": evt.get("out", "")[:200]
                })

            elif e_type == "fail":
                fail_count += 1
                tool_counts[evt.get("tool", "?")] += 1

        # Calculate duration
        duration_ms = 0
        if events:
            try:
                first_ts = datetime.fromisoformat(turn["ts"].replace("Z", "+00:00"))
                last_ts_str = events[-1].get("ts", turn["ts"])
                last_ts = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
                duration_ms = int((last_ts - first_ts).total_seconds() * 1000)
            except (ValueError, TypeError):
                pass

        turn["tools"] = {
            "total": sum(tool_counts.values()),
            "by_name": dict(tool_counts),
            "fail_count": fail_count
        }
        turn["delegates"] = delegates
        turn["bash_results"] = bash_results
        turn["grep_patterns"] = grep_patterns
        turn["files_touched"] = sorted(files_touched)
        turn["skills_used"] = skills_used
        turn["duration_ms"] = duration_ms

        return turn

    for evt in session_events:
        e_type = evt.get("e")

        if e_type == "turn":
            # Finalize previous turn
            if current_turn is not None:
                turns.append(finalize_turn(current_turn))

            # Start new turn
            current_turn = {
                "turn_idx": len(turns),
                "ts": evt.get("ts", ""),
                "prompt": evt.get("prompt", ""),
                "cwd": evt.get("cwd", ""),
                "_events": []
            }
        elif current_turn is not None:
            current_turn["_events"].append(evt)
        else:
            # Events before first turn (orphaned) - create implicit turn
            current_turn = {
                "turn_idx": len(turns),
                "ts": evt.get("ts", ""),
                "prompt": "(implicit - no user prompt recorded)",
                "cwd": "",
                "_events": [evt]
            }

    # Finalize last turn
    if current_turn is not None:
        turns.append(finalize_turn(current_turn))

    return turns


def compute_session_stats(turns: list[dict]) -> dict:
    """Compute aggregate statistics for a session."""
    total_tools = sum(t.get("tools", {}).get("total", 0) for t in turns)
    total_fails = sum(t.get("tools", {}).get("fail_count", 0) for t in turns)
    total_delegates = sum(len(t.get("delegates", [])) for t in turns)
    total_duration = sum(t.get("duration_ms", 0) for t in turns)

    all_files: set[str] = set()
    for t in turns:
        all_files.update(t.get("files_touched", []))

    return {
        "turn_count": len(turns),
        "total_tool_calls": total_tools,
        "total_failures": total_fails,
        "total_delegations": total_delegates,
        "total_duration_ms": total_duration,
        "unique_files": len(all_files)
    }


def build(input_path: str, output_path: str) -> dict:
    """Main build pipeline: JSONL -> episodes.json."""
    events = parse_jsonl(input_path)

    if not events:
        result = {
            "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "total_events": 0,
            "sessions": []
        }
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        return result

    sessions_raw = group_by_session(events)

    sessions = []
    for sid, session_events in sessions_raw.items():
        turns = build_turns(session_events)
        stats = compute_session_stats(turns)

        sessions.append({
            "session_id": sid,
            "stats": stats,
            "turns": turns
        })

    # Sort sessions by first turn timestamp
    sessions.sort(key=lambda s: s["turns"][0]["ts"] if s["turns"] else "")

    result = {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_events": len(events),
        "total_sessions": len(sessions),
        "total_turns": sum(s["stats"]["turn_count"] for s in sessions),
        "sessions": sessions
    }

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"Built {result['total_turns']} turns across {result['total_sessions']} sessions")
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="CL v3 Episode Builder (Stage 1)")
    parser.add_argument("--input", "-i", default=str(DEFAULT_INPUT),
                        help="Input JSONL file path")
    parser.add_argument("--output", "-o", default=str(DEFAULT_OUTPUT),
                        help="Output JSON file path")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    build(args.input, args.output)


if __name__ == "__main__":
    main()
