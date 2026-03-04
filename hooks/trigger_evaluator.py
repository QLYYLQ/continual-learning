#!/usr/bin/env python3
"""
Trigger Evaluator — Event-Driven Stage Pipeline Controller

Evaluates per-stage trigger rules from config.json, runs fast stages
synchronously, and queues LLM stages for daemon processing.

Usage:
    trigger_evaluator.py on_hook Stop          # Called by dispatcher on Stop event
    trigger_evaluator.py after_stage stage1    # Called by daemon after stage completes
    trigger_evaluator.py force_all             # Bypass trigger counts, queue all eligible
"""

import fcntl
import json
import os
import sys
import subprocess
from datetime import datetime, timezone
from pathlib import Path

CL_DIR = Path.home() / ".claude" / "continual-learning"
CONFIG_PATH = CL_DIR / "config.json"
STATE_DIR = CL_DIR / "data" / "state"
COUNTERS_PATH = STATE_DIR / "stage_counters.json"
QUEUE_PATH = STATE_DIR / "pending_stages.json"
INDEX_PATH = CL_DIR / "data" / "sessions" / "_index.json"
REGISTRY_PATH = CL_DIR / "data" / "task_registry.json"
OBSERVER_DIR = CL_DIR / "observer"
SESSIONS_DIR = CL_DIR / "data" / "sessions"


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(path))


def load_counters():
    default = {}
    for stage in ("stage1", "stage2", "stage3", "stage3b"):
        default[stage] = {
            "runs_since_trigger": 0,
            "total_runs": 0,
            "last_run": None,
        }
    counters = load_json(COUNTERS_PATH, default)
    for stage in ("stage1", "stage2", "stage3", "stage3b"):
        if stage not in counters:
            counters[stage] = {
                "runs_since_trigger": 0,
                "total_runs": 0,
                "last_run": None,
            }
    return counters


def load_queue():
    return load_json(QUEUE_PATH, [])


def save_queue(queue):
    save_json(QUEUE_PATH, queue)


def enqueue(stage_name):
    """Append to queue with file locking to avoid race with daemon drain."""
    queue_path = str(QUEUE_PATH)
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Ensure file exists
    if not QUEUE_PATH.exists():
        save_json(QUEUE_PATH, [])
    with open(queue_path, "r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            queue = json.load(f)
        except json.JSONDecodeError:
            queue = []
        if stage_name not in queue:
            queue.append(stage_name)
            f.seek(0)
            f.truncate()
            json.dump(queue, f)
            log(f"Queued {stage_name}")


def drain_queue():
    """Atomically read and clear the queue file (used by daemon)."""
    queue_path = str(QUEUE_PATH)
    try:
        with open(queue_path, "r+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                stages = json.load(f)
            except json.JSONDecodeError:
                stages = []
            f.seek(0)
            f.truncate()
            json.dump([], f)
        return stages
    except FileNotFoundError:
        return []


def increment_counter(counters, stage_name):
    now = datetime.now(timezone.utc).isoformat()
    counters[stage_name]["runs_since_trigger"] += 1
    counters[stage_name]["total_runs"] += 1
    counters[stage_name]["last_run"] = now
    save_json(COUNTERS_PATH, counters)


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] [trigger] {msg}", file=sys.stderr)


def evaluate_guard(guard, pipeline_config):
    """Check if guard conditions are met. Returns True if stage should be queued."""
    if not guard:
        return True

    if "min_turns" in guard:
        index = load_json(INDEX_PATH)
        total_turns = index.get("total_turns", 0)
        if total_turns < guard["min_turns"]:
            log(f"Guard failed: min_turns={guard['min_turns']}, have={total_turns}")
            return False

    if "min_dirty_tasks" in guard:
        registry = load_json(REGISTRY_PATH)
        dirty_count = len(registry.get("dirty_task_ids", []))
        if dirty_count < guard["min_dirty_tasks"]:
            log(f"Guard failed: min_dirty_tasks={guard['min_dirty_tasks']}, have={dirty_count}")
            return False

    if "min_bash_events" in guard:
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(OBSERVER_DIR / "extract_bash_contexts.py"),
                    "--task-registry", str(REGISTRY_PATH),
                    "--sessions-dir", str(SESSIONS_DIR),
                    "--output", "/dev/null",
                ],
                capture_output=True, text=True, timeout=30,
            )
            count = 0
            for line in result.stdout.splitlines():
                if "bash_context_count=" in line:
                    count = int(line.split("bash_context_count=")[1])
                    break
            if count < guard["min_bash_events"]:
                log(f"Guard failed: min_bash_events={guard['min_bash_events']}, have={count}")
                return False
        except Exception as e:
            log(f"Guard check error (min_bash_events): {e}")
            return False

    return True


def evaluate_downstream(completed_stage, counters, pipeline_config, force=False):
    """Find and queue stages triggered by completed_stage.

    The counter for completed_stage.runs_since_trigger tracks how many times
    the upstream stage has run since downstream stages were last triggered.
    The counter is reset once AFTER evaluating all downstream stages to avoid
    the first match resetting before the second is checked.
    """
    current_count = counters.get(completed_stage, {}).get("runs_since_trigger", 0)
    any_matched = False

    for stage_name, stage_cfg in pipeline_config.items():
        trigger = stage_cfg.get("trigger", {})
        if trigger.get("type") != "after_stage":
            continue
        if trigger.get("stage") != completed_stage:
            continue

        required_count = trigger.get("count", 1)

        if force or current_count >= required_count:
            any_matched = True
            guard = stage_cfg.get("guard", {})
            if evaluate_guard(guard, pipeline_config):
                enqueue(stage_name)
            else:
                log(f"Guard failed for {stage_name}")

    # Reset counter once after all downstream stages evaluated
    if any_matched:
        counters[completed_stage]["runs_since_trigger"] = 0
        save_json(COUNTERS_PATH, counters)


def run_stage1():
    """Run Stage 1 synchronously (session segmentation, no LLM)."""
    config = load_json(CONFIG_PATH)
    jsonl = CL_DIR / "data" / "turns.jsonl"
    index = SESSIONS_DIR / "_index.json"

    if not jsonl.exists():
        log("No turns.jsonl, skipping Stage 1")
        return False

    try:
        result = subprocess.run(
            [
                sys.executable,
                str(OBSERVER_DIR / "segment_sessions.py"),
                "--input", str(jsonl),
                "--outdir", str(SESSIONS_DIR),
                "--index", str(index),
                "--config", str(CONFIG_PATH),
            ],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            log("Stage 1 complete (session segmentation)")
            return True
        else:
            log(f"Stage 1 failed: {result.stderr[:200]}")
            return False
    except Exception as e:
        log(f"Stage 1 error: {e}")
        return False


def handle_on_hook(event_name):
    """Handle an on_hook trigger (called from Stop hook)."""
    config = load_json(CONFIG_PATH)
    pipeline = config.get("pipeline", {})
    counters = load_counters()

    # Find stages with on_hook trigger for this event
    for stage_name, stage_cfg in pipeline.items():
        trigger = stage_cfg.get("trigger", {})
        if trigger.get("type") == "on_hook" and trigger.get("event") == event_name:
            log(f"Running {stage_name} (on_hook {event_name})")

            if stage_name == "stage1":
                success = run_stage1()
                if success:
                    increment_counter(counters, stage_name)
                    evaluate_downstream(stage_name, counters, pipeline)


def handle_after_stage(stage_name):
    """Handle after_stage trigger (called by daemon after completing a stage)."""
    config = load_json(CONFIG_PATH)
    pipeline = config.get("pipeline", {})
    counters = load_counters()

    increment_counter(counters, stage_name)
    evaluate_downstream(stage_name, counters, pipeline)


def handle_force_all():
    """Bypass trigger counts, queue all stages whose guards pass."""
    config = load_json(CONFIG_PATH)
    pipeline = config.get("pipeline", {})
    counters = load_counters()

    # Run Stage 1 first
    if "stage1" in pipeline:
        success = run_stage1()
        if success:
            increment_counter(counters, "stage1")

    # Queue all downstream stages (force=True bypasses counts)
    for stage_name, stage_cfg in pipeline.items():
        trigger = stage_cfg.get("trigger", {})
        if trigger.get("type") == "after_stage":
            guard = stage_cfg.get("guard", {})
            if evaluate_guard(guard, pipeline):
                enqueue(stage_name)

    # Reset all counters
    for stage_name in counters:
        counters[stage_name]["runs_since_trigger"] = 0
    save_json(COUNTERS_PATH, counters)


def main():
    if len(sys.argv) < 2:
        print("Usage: trigger_evaluator.py {on_hook|after_stage|force_all} [args...]", file=sys.stderr)
        sys.exit(1)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (CL_DIR / "data" / "log").mkdir(parents=True, exist_ok=True)

    command = sys.argv[1]

    if command == "on_hook":
        event_name = sys.argv[2] if len(sys.argv) > 2 else "Stop"
        handle_on_hook(event_name)
    elif command == "after_stage":
        if len(sys.argv) < 3:
            print("Usage: trigger_evaluator.py after_stage <stage_name>", file=sys.stderr)
            sys.exit(1)
        handle_after_stage(sys.argv[2])
    elif command == "force_all":
        handle_force_all()
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
