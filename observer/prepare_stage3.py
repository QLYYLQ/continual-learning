#!/usr/bin/env python3
"""
Continuous Learning v3 - Stage 3 Pre-processor

Reads dirty_task_ids from task_registry.json, stitches fragment turns
chronologically with _session_break markers, writes .stage3_bundle.json.

Exit codes:
  0 — dirty tasks found, bundle written
  2 — no dirty tasks to process

Usage:
  python3 prepare_stage3.py --registry data/task_registry.json \
      --sessions-dir data/sessions \
      --output data/.stage3_bundle.json
"""

import argparse
import json
import sys
from pathlib import Path


def load_json(path: str, default: dict | None = None) -> dict:
    """Load JSON file, return default if missing or malformed."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def load_session(sessions_dir: str, sid: str) -> dict | None:
    """Load a session file by SID."""
    path = Path(sessions_dir) / f"{sid}.json"
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"Warning: could not load session {sid}", file=sys.stderr)
        return None


def extract_fragment_turns(session: dict, turn_range: list[int]) -> list[dict]:
    """Extract turns within the specified range from a session."""
    turns = session.get("turns", [])
    if len(turn_range) != 2:
        return turns

    start_idx, end_idx = turn_range
    result = []
    for turn in turns:
        idx = turn.get("turn_idx", -1)
        if start_idx <= idx <= end_idx:
            result.append(turn)
    return result


def build_task_trajectory(task: dict, sessions_dir: str) -> list[dict]:
    """Build chronological trajectory from all fragments, with session break markers."""
    fragments = task.get("fragments", [])
    trajectory: list[dict] = []
    prev_end_ts = None

    for i, fragment in enumerate(fragments):
        sid = fragment.get("sid", "")
        turn_range = fragment.get("turn_range", [])

        session = load_session(sessions_dir, sid)
        if session is None:
            continue

        turns = extract_fragment_turns(session, turn_range)
        if not turns:
            continue

        # Insert session break marker between fragments
        if i > 0 and prev_end_ts:
            first_ts = turns[0].get("ts", "")
            gap_minutes = None
            if first_ts and prev_end_ts:
                try:
                    from datetime import datetime
                    curr = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
                    prev = datetime.fromisoformat(prev_end_ts.replace("Z", "+00:00"))
                    gap_minutes = round((curr - prev).total_seconds() / 60.0, 1)
                except (ValueError, TypeError):
                    pass
            trajectory.append({
                "_session_break": True,
                "gap_minutes": gap_minutes,
            })

        # Add turns with session context
        for turn in turns:
            entry = {
                "sid": sid,
                **turn,
            }
            trajectory.append(entry)

        # Track end timestamp for gap calculation
        if turns:
            last_turn = turns[-1]
            prev_end_ts = last_turn.get("ts", "")

    return trajectory


def main() -> None:
    parser = argparse.ArgumentParser(description="CL v3 Stage 3 Pre-processor")
    parser.add_argument("--registry", required=True, help="Path to task_registry.json")
    parser.add_argument("--sessions-dir", required=True, help="Path to sessions directory")
    parser.add_argument("--output", required=True, help="Output bundle path")
    args = parser.parse_args()

    registry = load_json(args.registry)
    dirty_ids = registry.get("dirty_task_ids", [])
    tasks = registry.get("tasks", {})

    if not dirty_ids:
        print("dirty_count=0")
        sys.exit(2)

    dirty_tasks = []
    for task_id in dirty_ids:
        task = tasks.get(task_id)
        if task is None:
            print(f"Warning: dirty task {task_id} not found in registry", file=sys.stderr)
            continue

        trajectory = build_task_trajectory(task, args.sessions_dir)
        dirty_tasks.append({
            "task_id": task_id,
            "name": task.get("name", ""),
            "task_type": task.get("task_type", ""),
            "status": task.get("status", "active"),
            "trajectory": trajectory,
        })

    if not dirty_tasks:
        print("dirty_count=0")
        sys.exit(2)

    bundle = {"dirty_tasks": dirty_tasks}
    with open(args.output, "w") as f:
        json.dump(bundle, f, indent=2, ensure_ascii=False)

    print(f"dirty_count={len(dirty_tasks)}")


if __name__ == "__main__":
    main()
