#!/usr/bin/env python3
"""
Continuous Learning v3 - Stage 2 Pre-processor

Diffs _index.json against .stage2_cursor.json to identify new/changed sessions.
Writes a manifest file for the Stage 2 LLM with new session paths and
existing task summaries.

Exit codes:
  0 — new sessions found, manifest written
  2 — no new sessions to process

Usage:
  python3 prepare_stage2.py --index data/sessions/_index.json \
      --cursor data/.stage2_cursor.json \
      --registry data/task_registry.json \
      --sessions-dir data/sessions \
      --manifest data/.stage2_manifest.json \
      [--start-time 2026-02-16T00:00:00Z] \
      [--end-time 2026-02-19T23:59:59Z]
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

CL_DIR = Path.home() / ".claude" / "continual-learning"


def load_json(path: str, default: dict | None = None) -> dict:
    """Load JSON file, return default if missing or malformed."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def parse_iso(ts_str: str) -> datetime | None:
    """Parse ISO timestamp string, return None on failure."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="CL v3 Stage 2 Pre-processor")
    parser.add_argument("--index", required=True, help="Path to _index.json")
    parser.add_argument("--cursor", required=True, help="Path to .stage2_cursor.json")
    parser.add_argument("--registry", required=True, help="Path to task_registry.json")
    parser.add_argument("--sessions-dir", required=True, help="Path to sessions directory")
    parser.add_argument("--manifest", required=True, help="Output manifest path")
    parser.add_argument("--start-time", default=None, help="Filter: only sessions starting at or after this ISO time")
    parser.add_argument("--end-time", default=None, help="Filter: only sessions starting at or before this ISO time")
    args = parser.parse_args()

    index = load_json(args.index)
    cursor = load_json(args.cursor, {"processed_sessions": {}})
    registry = load_json(args.registry, {"tasks": {}, "non_tasks": []})

    processed = cursor.get("processed_sessions", {})
    sessions = index.get("sessions", [])

    # Parse time filters
    start_filter = parse_iso(args.start_time) if args.start_time else None
    end_filter = parse_iso(args.end_time) if args.end_time else None

    # Find new/changed sessions
    new_sessions = []
    for entry in sessions:
        sid = entry["sid"]
        event_count = entry.get("event_count", 0)

        # Check cursor: new or grown
        prev = processed.get(sid)
        if prev is not None and event_count <= prev.get("event_count", 0):
            continue

        # Apply time filters on session start
        if start_filter or end_filter:
            session_start = parse_iso(entry.get("start", ""))
            if session_start:
                if start_filter and session_start < start_filter:
                    continue
                if end_filter and session_start > end_filter:
                    continue
            else:
                # No timestamp — skip when filtering
                continue

        session_path = str(Path(args.sessions_dir) / f"{sid}.json")
        new_sessions.append({
            "sid": sid,
            "path": session_path,
            "start": entry.get("start", ""),
            "primary_cwd": entry.get("primary_cwd", ""),
        })

    if not new_sessions:
        print("new_count=0")
        sys.exit(2)

    # Build existing task summaries (compact for LLM context)
    tasks = registry.get("tasks", {})
    task_summaries = []
    for task_id, task in tasks.items():
        summary = {
            "task_id": task_id,
            "name": task.get("name", ""),
            "description": task.get("description", ""),
            "status": task.get("status", "active"),
            "primary_cwd": task.get("primary_cwd", ""),
            "fragment_count": len(task.get("fragments", [])),
        }
        task_summaries.append(summary)

    # Sort task summaries by task_id for stable output
    task_summaries.sort(key=lambda t: t["task_id"])

    manifest = {
        "new_sessions": new_sessions,
        "existing_task_summaries": task_summaries,
        "registry_path": str(Path(args.registry).resolve()),
        "sessions_dir": str(Path(args.sessions_dir).resolve()),
    }

    with open(args.manifest, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"new_count={len(new_sessions)}")


if __name__ == "__main__":
    main()
