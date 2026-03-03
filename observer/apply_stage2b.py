#!/usr/bin/env python3
"""
Continuous Learning v4 - Stage 2b Post-processor

Applies cross-session merging operations from Stage 2b LLM to task_registry.json.
Updates dirty_task_ids and .stage2_cursor.json.

Input:  cache/stage2b_ops.json + cache/stage2b_candidates.json + task_registry.json
Output: Updated task_registry.json + .stage2_cursor.json
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def load_json(path: str, default: dict | None = None) -> dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def atomic_write(path: str, data: dict) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def next_task_id(registry: dict) -> str:
    num = registry.get("next_task_num", 1)
    registry["next_task_num"] = num + 1
    return f"task-{num:03d}"


def main() -> None:
    parser = argparse.ArgumentParser(description="CL v4 Stage 2b Post-processor")
    parser.add_argument("--ops", required=True, help="Path to LLM ops JSON (stage2b_ops.json)")
    parser.add_argument("--candidates", required=True, help="Path to stage2b_candidates.json")
    parser.add_argument("--registry", required=True, help="Path to task_registry.json")
    parser.add_argument("--cursor", required=True, help="Path to .stage2_cursor.json")
    parser.add_argument("--sessions-dir", required=True, help="Path to sessions directory")
    parser.add_argument("--manifest", required=True, help="Path to stage2a_manifest.json (for cursor update)")
    args = parser.parse_args()

    ops_data = load_json(args.ops)
    candidates_data = load_json(args.candidates)
    registry = load_json(args.registry, {
        "version": 1,
        "updated_at": "",
        "dirty_task_ids": [],
        "next_task_num": 1,
        "tasks": {},
        "non_tasks": [],
    })
    manifest = load_json(args.manifest)

    # Ensure required fields
    registry.setdefault("version", 1)
    registry.setdefault("next_task_num", 1)
    registry.setdefault("dirty_task_ids", [])
    registry.setdefault("tasks", {})
    registry.setdefault("non_tasks", [])

    tasks = registry["tasks"]
    non_tasks = registry["non_tasks"]
    dirty = set(registry.get("dirty_task_ids", []))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build candidate lookup
    cand_map = {}
    for c in candidates_data.get("new_candidates", []):
        cand_map[c["candidate_id"]] = c

    operations = ops_data.get("operations", [])

    for op in operations:
        op_type = op.get("op", "")

        if op_type == "create_task":
            cand_id = op.get("candidate_id", "")
            cand = cand_map.get(cand_id, {})
            fragments = cand.get("fragments", [])

            # Add role to fragments
            for frag in fragments:
                frag.setdefault("role", "origin")

            task_id = next_task_id(registry)

            # Determine primary_cwd from candidate
            primary_cwd = cand.get("primary_cwd", "")

            # Get created_at from session
            created_at = now
            if fragments:
                sid = fragments[0].get("sid", "")
                session_path = os.path.join(args.sessions_dir, f"{sid}.json")
                try:
                    with open(session_path) as f:
                        session = json.load(f)
                    created_at = session.get("time_range", {}).get("start", now)
                except (FileNotFoundError, json.JSONDecodeError):
                    pass

            tasks[task_id] = {
                "task_id": task_id,
                "name": op.get("name", cand.get("name", "Unnamed task")),
                "description": op.get("description", cand.get("description", "")),
                "task_type": op.get("task_type", cand.get("task_type", "feature")),
                "status": op.get("status", "active"),
                "primary_cwd": primary_cwd,
                "created_at": created_at,
                "updated_at": now,
                "fragments": fragments,
                "relations": [],
            }
            dirty.add(task_id)

        elif op_type == "append_to_existing":
            cand_id = op.get("candidate_id", "")
            target_id = op.get("target_task_id", "")
            cand = cand_map.get(cand_id, {})

            if target_id not in tasks:
                print(f"Warning: append_to_existing - task {target_id} not found, creating new task",
                      file=sys.stderr)
                # Fallback: create as new task
                task_id = next_task_id(registry)
                fragments = cand.get("fragments", [])
                for frag in fragments:
                    frag.setdefault("role", "origin")
                tasks[task_id] = {
                    "task_id": task_id,
                    "name": cand.get("name", "Unnamed"),
                    "description": cand.get("description", ""),
                    "task_type": cand.get("task_type", "feature"),
                    "status": "active",
                    "primary_cwd": cand.get("primary_cwd", ""),
                    "created_at": now,
                    "updated_at": now,
                    "fragments": fragments,
                    "relations": [],
                }
                dirty.add(task_id)
                continue

            fragments = cand.get("fragments", [])
            role = op.get("fragment_role", "continuation")
            for frag in fragments:
                frag["role"] = role
                tasks[target_id]["fragments"].append(frag)

            tasks[target_id]["updated_at"] = now
            updated_desc = op.get("updated_description", "")
            if updated_desc:
                tasks[target_id]["description"] = updated_desc
            dirty.add(target_id)

        elif op_type == "merge_candidates":
            cand_ids = op.get("candidate_ids", [])
            all_fragments = []
            primary_cwd = ""
            for cid in cand_ids:
                c = cand_map.get(cid, {})
                for frag in c.get("fragments", []):
                    frag.setdefault("role", "origin")
                    all_fragments.append(frag)
                if not primary_cwd:
                    primary_cwd = c.get("primary_cwd", "")

            task_id = next_task_id(registry)
            tasks[task_id] = {
                "task_id": task_id,
                "name": op.get("name", "Merged task"),
                "description": op.get("description", ""),
                "task_type": op.get("task_type", "feature"),
                "status": op.get("status", "active"),
                "primary_cwd": primary_cwd,
                "created_at": now,
                "updated_at": now,
                "fragments": all_fragments,
                "relations": [],
            }
            dirty.add(task_id)

        elif op_type == "mark_non_task":
            cand_id = op.get("candidate_id", "")
            cand = cand_map.get(cand_id, {})
            for frag in cand.get("fragments", []):
                non_tasks.append({
                    "sid": frag.get("sid", ""),
                    "reason": op.get("reason", ""),
                })

        elif op_type == "update_status":
            task_id = op.get("task_id", "")
            if task_id in tasks:
                tasks[task_id]["status"] = op.get("status", "active")
                tasks[task_id]["updated_at"] = now
                dirty.add(task_id)

        elif op_type == "add_relation":
            from_id = op.get("from_id", "")
            to_id = op.get("to_id", "")
            relation = op.get("relation", "related")
            if from_id in tasks and to_id in tasks:
                tasks[from_id]["relations"].append({
                    "task_id": to_id,
                    "relation": relation,
                })
                tasks[from_id]["updated_at"] = now

        else:
            print(f"Warning: unknown op '{op_type}', skipping", file=sys.stderr)

    registry["dirty_task_ids"] = sorted(dirty)
    registry["updated_at"] = now
    atomic_write(args.registry, registry)

    # Update cursor
    cursor = load_json(args.cursor, {"processed_sessions": {}})
    processed = cursor.setdefault("processed_sessions", {})
    for session in manifest.get("new_sessions", []):
        sid = session["sid"]
        session_path = os.path.join(args.sessions_dir, f"{sid}.json")
        event_count = 0
        try:
            with open(session_path) as f:
                data = json.load(f)
            event_count = data.get("event_count", 0)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        processed[sid] = {
            "event_count": event_count,
            "processed_at": now,
        }
    atomic_write(args.cursor, cursor)

    task_count = len(tasks)
    print(f"Applied {len(operations)} operations, "
          f"{task_count} tasks total, {len(dirty)} dirty", file=sys.stderr)


if __name__ == "__main__":
    main()
