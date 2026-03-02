#!/usr/bin/env python3
"""
Continuous Learning v3 - Stage 2 Post-processor

Validates and applies LLM-generated operations to task_registry.json.
Updates .stage2_cursor.json to mark processed sessions.

Supported operations:
  create_task, append_fragment, split_session, merge_tasks,
  mark_non_task, update_status, add_relation

Usage:
  python3 apply_stage2.py --ops data/.stage2_ops.json \
      --registry data/task_registry.json \
      --cursor data/.stage2_cursor.json \
      --manifest data/.stage2_manifest.json
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def load_json(path: str, default: dict | None = None) -> dict:
    """Load JSON file, return default if missing or malformed."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def atomic_write(path: str, data: dict) -> None:
    """Write JSON atomically via tmp + rename."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def next_task_id(registry: dict) -> str:
    """Generate next task ID and increment counter."""
    num = registry.get("next_task_num", 1)
    registry["next_task_num"] = num + 1
    return f"task-{num:03d}"


def find_primary_cwd(fragments: list[dict], sessions_dir: str) -> str:
    """Determine primary_cwd from the first fragment's session file."""
    if not fragments:
        return ""
    first_frag = fragments[0]
    sid = first_frag.get("sid", "")
    session_path = os.path.join(sessions_dir, f"{sid}.json")
    try:
        with open(session_path, "r") as f:
            session = json.load(f)
        return session.get("primary_cwd", "")
    except (FileNotFoundError, json.JSONDecodeError):
        return ""


def apply_operations(ops_data: dict, registry: dict, manifest: dict) -> list[str]:
    """Apply operations to registry, return list of dirty task IDs."""
    operations = ops_data.get("operations", [])
    tasks = registry.setdefault("tasks", {})
    non_tasks = registry.setdefault("non_tasks", [])
    dirty = set(registry.get("dirty_task_ids", []))
    sessions_dir = manifest.get("sessions_dir", "")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for op in operations:
        op_type = op.get("op", "")

        if op_type == "create_task":
            task_id = next_task_id(registry)
            fragments = op.get("fragments", [])
            primary_cwd = find_primary_cwd(fragments, sessions_dir)

            # Get created_at from the first fragment's start time
            created_at = now
            if fragments:
                first_sid = fragments[0].get("sid", "")
                session_path = os.path.join(sessions_dir, f"{first_sid}.json")
                try:
                    with open(session_path, "r") as f:
                        session = json.load(f)
                    created_at = session.get("time_range", {}).get("start", now)
                except (FileNotFoundError, json.JSONDecodeError):
                    pass

            tasks[task_id] = {
                "task_id": task_id,
                "name": op.get("name", "Unnamed task"),
                "description": op.get("description", ""),
                "task_type": op.get("task_type", "feature"),
                "status": op.get("status", "active"),
                "primary_cwd": primary_cwd,
                "created_at": created_at,
                "updated_at": now,
                "fragments": fragments,
                "relations": [],
            }
            dirty.add(task_id)

        elif op_type == "append_fragment":
            task_id = op.get("task_id", "")
            if task_id not in tasks:
                print(f"Warning: append_fragment - task {task_id} not found, skipping", file=sys.stderr)
                continue
            fragment = op.get("fragment", {})
            tasks[task_id]["fragments"].append(fragment)
            tasks[task_id]["updated_at"] = now
            # Extend description if LLM provided an update
            updated_desc = op.get("updated_description", "")
            if updated_desc:
                tasks[task_id]["description"] = updated_desc
            dirty.add(task_id)

        elif op_type == "split_session":
            assignments = op.get("assignments", [])
            for assignment in assignments:
                target_id = assignment.get("task_id", "")
                turn_range = assignment.get("turn_range", [])
                sid = op.get("sid", "")

                fragment = {
                    "sid": sid,
                    "turn_range": turn_range,
                    "role": assignment.get("role", "origin"),
                }

                if target_id and target_id in tasks:
                    # Append to existing task
                    tasks[target_id]["fragments"].append(fragment)
                    tasks[target_id]["updated_at"] = now
                    updated_desc = assignment.get("updated_description", "")
                    if updated_desc:
                        tasks[target_id]["description"] = updated_desc
                    dirty.add(target_id)
                elif assignment.get("new_task_name"):
                    # Create new task for this split
                    new_id = next_task_id(registry)
                    primary_cwd = find_primary_cwd([fragment], sessions_dir)
                    tasks[new_id] = {
                        "task_id": new_id,
                        "name": assignment["new_task_name"],
                        "description": assignment.get("description", ""),
                        "task_type": assignment.get("task_type", "feature"),
                        "status": assignment.get("status", "active"),
                        "primary_cwd": primary_cwd,
                        "created_at": now,
                        "updated_at": now,
                        "fragments": [fragment],
                        "relations": [],
                    }
                    dirty.add(new_id)

        elif op_type == "merge_tasks":
            source_id = op.get("source_id", "")
            target_id = op.get("target_id", "")
            if source_id not in tasks:
                print(f"Warning: merge_tasks - source {source_id} not found, skipping", file=sys.stderr)
                continue
            if target_id not in tasks:
                print(f"Warning: merge_tasks - target {target_id} not found, skipping", file=sys.stderr)
                continue
            # Move fragments from source to target
            source_frags = tasks[source_id].get("fragments", [])
            tasks[target_id]["fragments"].extend(source_frags)
            tasks[target_id]["updated_at"] = now
            # Move relations
            source_relations = tasks[source_id].get("relations", [])
            for rel in source_relations:
                if rel.get("task_id") != target_id:
                    tasks[target_id]["relations"].append(rel)
            # Delete source task
            del tasks[source_id]
            dirty.discard(source_id)
            dirty.add(target_id)

        elif op_type == "mark_non_task":
            sid = op.get("sid", "")
            reason = op.get("reason", "")
            non_tasks.append({"sid": sid, "reason": reason})

        elif op_type == "update_status":
            task_id = op.get("task_id", "")
            if task_id not in tasks:
                print(f"Warning: update_status - task {task_id} not found, skipping", file=sys.stderr)
                continue
            tasks[task_id]["status"] = op.get("status", "active")
            tasks[task_id]["updated_at"] = now
            dirty.add(task_id)

        elif op_type == "add_relation":
            from_id = op.get("from_id", "")
            to_id = op.get("to_id", "")
            relation = op.get("relation", "related")
            if from_id not in tasks:
                print(f"Warning: add_relation - from_id {from_id} not found, skipping", file=sys.stderr)
                continue
            if to_id not in tasks:
                print(f"Warning: add_relation - to_id {to_id} not found, skipping", file=sys.stderr)
                continue
            tasks[from_id]["relations"].append({"task_id": to_id, "relation": relation})
            tasks[from_id]["updated_at"] = now

        else:
            print(f"Warning: unknown operation type '{op_type}', skipping", file=sys.stderr)

    registry["dirty_task_ids"] = sorted(dirty)
    registry["updated_at"] = now
    return sorted(dirty)


def update_cursor(cursor_path: str, manifest: dict) -> None:
    """Update cursor to mark manifest sessions as processed."""
    cursor = load_json(cursor_path, {"processed_sessions": {}})
    processed = cursor.setdefault("processed_sessions", {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for session in manifest.get("new_sessions", []):
        sid = session["sid"]
        # Read actual event_count from session file
        session_path = session.get("path", "")
        event_count = 0
        try:
            with open(session_path, "r") as f:
                data = json.load(f)
            event_count = data.get("event_count", 0)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        processed[sid] = {
            "event_count": event_count,
            "processed_at": now,
        }

    atomic_write(cursor_path, cursor)


def main() -> None:
    parser = argparse.ArgumentParser(description="CL v3 Stage 2 Post-processor")
    parser.add_argument("--ops", required=True, help="Path to LLM ops JSON")
    parser.add_argument("--registry", required=True, help="Path to task_registry.json")
    parser.add_argument("--cursor", required=True, help="Path to .stage2_cursor.json")
    parser.add_argument("--manifest", required=True, help="Path to .stage2_manifest.json")
    args = parser.parse_args()

    ops_data = load_json(args.ops)
    registry = load_json(args.registry, {
        "version": 1,
        "updated_at": "",
        "dirty_task_ids": [],
        "next_task_num": 1,
        "tasks": {},
        "non_tasks": [],
    })
    manifest = load_json(args.manifest)

    # Ensure registry has required fields
    registry.setdefault("version", 1)
    registry.setdefault("next_task_num", 1)
    registry.setdefault("dirty_task_ids", [])
    registry.setdefault("tasks", {})
    registry.setdefault("non_tasks", [])

    dirty = apply_operations(ops_data, registry, manifest)
    atomic_write(args.registry, registry)
    update_cursor(args.cursor, manifest)

    task_count = len(registry["tasks"])
    print(f"Applied {len(ops_data.get('operations', []))} operations, "
          f"{task_count} tasks total, {len(dirty)} dirty", file=sys.stderr)


if __name__ == "__main__":
    main()
