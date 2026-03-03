#!/usr/bin/env python3
"""
Continuous Learning v4 - Stage 2a Post-processor

Reads LLM-generated session segments, combines with existing task summaries,
and writes candidate tasks for Stage 2b (cross-session merging).

Input:  cache/stage2a_ops.json (LLM output) + task_registry.json
Output: cache/stage2b_candidates.json
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def load_json(path: str, default: dict | None = None) -> dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def main() -> None:
    parser = argparse.ArgumentParser(description="CL v4 Stage 2a Post-processor")
    parser.add_argument("--ops", required=True, help="Path to LLM ops JSON (stage2a_ops.json)")
    parser.add_argument("--registry", required=True, help="Path to task_registry.json")
    parser.add_argument("--manifest", required=True, help="Path to stage2a_manifest.json")
    parser.add_argument("--output", required=True, help="Output path for stage2b_candidates.json")
    args = parser.parse_args()

    ops_data = load_json(args.ops)
    registry = load_json(args.registry, {"tasks": {}, "non_tasks": []})
    manifest = load_json(args.manifest)

    session_segments = ops_data.get("session_segments", [])

    # Build lookup: sid -> session info from manifest
    session_info = {}
    for s in manifest.get("new_sessions", []):
        session_info[s["sid"]] = s

    # Generate candidate tasks from segments
    candidates = []
    cand_num = 0
    for ss in session_segments:
        sid = ss.get("sid", "")
        segments = ss.get("segments", [])
        info = session_info.get(sid, {})

        for seg in segments:
            cand_num += 1
            cand_id = f"cand-{cand_num:03d}"
            candidates.append({
                "candidate_id": cand_id,
                "name": seg.get("name", "Unnamed segment"),
                "task_type": seg.get("task_type", "feature"),
                "description": seg.get("description", ""),
                "fragments": [{"sid": sid, "turn_range": seg.get("turn_range", [])}],
                "primary_cwd": info.get("primary_cwd", ""),
                "time_range": {
                    "start": info.get("start", ""),
                    "end": "",  # Will be filled from session data if needed
                },
            })

    # Build existing task summaries
    tasks = registry.get("tasks", {})
    existing_tasks = []
    for task_id, task in sorted(tasks.items()):
        existing_tasks.append({
            "task_id": task_id,
            "name": task.get("name", ""),
            "description": task.get("description", ""),
            "status": task.get("status", "active"),
            "primary_cwd": task.get("primary_cwd", ""),
            "fragment_count": len(task.get("fragments", [])),
        })

    output = {
        "new_candidates": candidates,
        "existing_tasks": existing_tasks,
    }
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"candidate_count={len(candidates)}", file=sys.stderr)


if __name__ == "__main__":
    main()
