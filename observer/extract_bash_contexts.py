#!/usr/bin/env python3
"""
Continuous Learning v3 - Bash Context Extractor (Stage 3b pre-processing)

Task-driven mode: extracts bash command contexts only for dirty tasks,
with enriched trajectory context (what the user was doing before/after
each bash call).

Input:  task_registry.json + data/sessions/{sid}.json files
Output: data/.stage3b_task_contexts.json

Usage:
  # Task-driven extraction (normal mode)
  python3 extract_bash_contexts.py --task-registry data/task_registry.json \
      --sessions-dir data/sessions --output data/.stage3b_task_contexts.json
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
        return None


def get_fragment_turns(session: dict, turn_range: list[int]) -> list[dict]:
    """Get turns within the specified range."""
    turns = session.get("turns", [])
    if len(turn_range) != 2:
        return turns
    start_idx, end_idx = turn_range
    return [t for t in turns if start_idx <= t.get("turn_idx", -1) <= end_idx]


def turn_summary(turn: dict) -> dict:
    """Create a compact summary of a turn for trajectory context."""
    return {
        "turn_idx": turn.get("turn_idx", -1),
        "prompt_preview": turn.get("prompt", "")[:100],
        "tools": turn.get("tools", {}),
    }


def find_bash_events_in_session(session: dict) -> list[dict]:
    """Find all raw events that are bash tool calls in a session.

    Returns list of dicts with event info and the session's raw event list index.
    We reconstruct from turns since we don't have raw events in session files.
    """
    # In the new format, turns have bash_commands list but not individual events.
    # We can identify bash activity from turns that have Bash in their tools dict.
    return []


def extract_task_bash_contexts(task: dict, sessions_dir: str) -> list[dict]:
    """Extract bash contexts for a single task from its fragments.

    For each turn that contains bash commands, build enriched context with
    trajectory before/after.
    """
    fragments = task.get("fragments", [])
    contexts: list[dict] = []

    # Build full turn list across fragments
    all_turns: list[dict] = []
    for fragment in fragments:
        sid = fragment.get("sid", "")
        turn_range = fragment.get("turn_range", [])
        session = load_session(sessions_dir, sid)
        if session is None:
            continue
        turns = get_fragment_turns(session, turn_range)
        for turn in turns:
            all_turns.append({**turn, "_sid": sid})

    # For each turn with bash commands, extract context
    for i, turn in enumerate(all_turns):
        bash_cmds = turn.get("bash_commands", [])
        has_bash = turn.get("tools", {}).get("Bash", 0) > 0
        if not bash_cmds and not has_bash:
            continue

        sid = turn.get("_sid", "")
        fail_count = turn.get("fail_count", 0)

        # Trajectory before: 1-2 preceding turns
        traj_before = []
        start = max(0, i - 2)
        for j in range(start, i):
            traj_before.append(turn_summary(all_turns[j]))

        # Trajectory after: 1 following turn
        traj_after = []
        if i + 1 < len(all_turns):
            traj_after.append(turn_summary(all_turns[i + 1]))

        # Build bash call info from available data
        for cmd_idx, cmd in enumerate(bash_cmds):
            bash_call = {
                "command": cmd,
                "ts": turn.get("ts", ""),
            }

            # Determine failure status
            has_failure = fail_count > 0

            # Correction candidate: if the next turn also has bash commands
            # (different commands = implicit correction)
            correction_candidate = False
            if has_failure:
                correction_candidate = True
            elif i + 1 < len(all_turns):
                next_bash = all_turns[i + 1].get("bash_commands", [])
                if next_bash and next_bash != bash_cmds:
                    correction_candidate = True

            context = {
                "sid": sid,
                "turn_idx": turn.get("turn_idx", -1),
                "trajectory_before": traj_before,
                "bash_call": bash_call,
                "feedback": {
                    "type": "fail" if has_failure else "bash_ok",
                    "output_preview": "",
                },
                "trajectory_after": traj_after,
                "has_failure": has_failure,
                "correction_candidate": correction_candidate,
            }
            contexts.append(context)

    return contexts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CL v3 Bash Context Extractor (Stage 3b, task-driven)"
    )
    parser.add_argument(
        "--task-registry", required=True,
        help="Path to task_registry.json",
    )
    parser.add_argument(
        "--sessions-dir", required=True,
        help="Path to sessions directory",
    )
    parser.add_argument(
        "--output", required=True,
        help="Path to output JSON file",
    )
    args = parser.parse_args()

    registry = load_json(args.task_registry)
    dirty_ids = registry.get("dirty_task_ids", [])
    tasks = registry.get("tasks", {})

    if not dirty_ids:
        # Write empty output
        with open(args.output, "w") as f:
            json.dump({"tasks": []}, f)
        print("bash_context_count=0")
        return

    result_tasks = []
    total_contexts = 0

    for task_id in dirty_ids:
        task = tasks.get(task_id)
        if task is None:
            continue

        contexts = extract_task_bash_contexts(task, args.sessions_dir)
        if not contexts:
            continue

        total_contexts += len(contexts)
        result_tasks.append({
            "task_id": task_id,
            "name": task.get("name", ""),
            "task_type": task.get("task_type", ""),
            "bash_contexts": contexts,
        })

    output = {"tasks": result_tasks}
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"bash_context_count={total_contexts}")


if __name__ == "__main__":
    main()
