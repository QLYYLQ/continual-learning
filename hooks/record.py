#!/usr/bin/env python3
"""
Continuous Learning v4 - Hook recorder (Python)

Full-fidelity recording: appends structured JSONL events to turns.jsonl
with NO truncation. Reads stdin directly (no bash variable size limits).

Replaces record.sh — invoked by dispatcher.sh.

Usage:
  echo '{"tool_name":"Bash",...}' | record.py user_prompt
  echo '{"tool_name":"Bash",...}' | record.py pre_tool
  echo '{"tool_name":"Bash",...}' | record.py bash_result
  echo '{"tool_name":"Bash",...}' | record.py tool_fail
  echo '{"tool_name":"Bash",...}' | record.py stop
  echo '{"tool_name":"Agent",...}' | record.py subagent_start
  echo '{"tool_name":"Agent",...}' | record.py subagent_stop
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

CL_DIR = Path.home() / ".claude" / "continual-learning"
DATA_DIR = CL_DIR / "data"
JSONL = DATA_DIR / "turns.jsonl"
CONFIG = CL_DIR / "config.json"


def load_config() -> dict:
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except Exception:
        return {}


def rotate_if_needed(config: dict) -> None:
    """Rotate turns.jsonl if it exceeds max_file_size_mb."""
    max_size = config.get("data", {}).get("max_file_size_mb", 50) * 1024 * 1024
    try:
        current_size = os.path.getsize(JSONL)
    except OSError:
        return

    if current_size >= max_size:
        archive_dir = DATA_DIR / "turns.archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_name = datetime.now().strftime("%Y%m%d-%H%M%S") + ".jsonl"
        try:
            os.rename(JSONL, archive_dir / archive_name)
        except OSError:
            pass  # Another hook already rotated


def build_record(hook_type: str, data: dict, config: dict) -> dict | None:
    """Build the JSONL record from hook payload. No truncation."""
    tool_cfg = config.get("tool_recording", {})
    ignore_tools = set(tool_cfg.get("ignore", []))

    tool = data.get("tool_name", "")
    inp = data.get("tool_input", {})

    if hook_type == "user_prompt":
        prompt = data.get("prompt", "")
        if not prompt:
            prompt = data.get("message", data.get("content", ""))
        return {
            "e": "turn",
            "prompt": str(prompt),
            "cwd": data.get("cwd", os.getcwd()),
        }

    elif hook_type == "pre_tool":
        if not tool or tool in ignore_tools:
            return None

        overrides = tool_cfg.get("overrides", {})
        default_cfg = tool_cfg.get("default", {})
        tool_override = overrides.get(tool, {})
        input_mode = tool_override.get("input", default_cfg.get("input", "target_only"))

        if input_mode == "delegate":
            return {
                "e": "delegate",
                "agent": inp.get("subagent_type", "?"),
                "agent_prompt": str(inp.get("prompt", "")),
            }
        elif input_mode == "skill":
            return {
                "e": "skill",
                "skill": inp.get("skill", "?"),
                "args": str(inp.get("args", "")),
            }
        elif input_mode == "detailed":
            rec = {"e": "tool", "tool": tool}
            fields = tool_override.get("fields", [])
            for field in fields:
                val = inp.get(field, "")
                if val:
                    rec[field] = str(val)
            # Also store full input for Bash commands
            if tool == "Bash":
                rec["input"] = {k: str(v) for k, v in inp.items() if v}
            return rec
        else:
            # target_only
            rec = {"e": "tool", "tool": tool}
            target_fields = default_cfg.get("target_fields", ["file_path", "pattern", "url"])
            for field in target_fields:
                val = inp.get(field, "")
                if val:
                    rec["target"] = str(val)
                    break
            return rec

    elif hook_type == "bash_result":
        resp = data.get("tool_response", "")
        cmd = str(inp.get("command", data.get("tool_input", {}).get("command", "")))
        return {
            "e": "bash_ok",
            "cmd": cmd,
            "out": str(resp),
        }

    elif hook_type == "tool_fail":
        if tool in ignore_tools:
            return None
        return {
            "e": "fail",
            "tool": tool,
            "error": str(data.get("tool_response", data.get("error", ""))),
        }

    elif hook_type == "stop":
        rec = {"e": "stop"}
        # Capture last_assistant_message if available
        last_msg = data.get("last_assistant_message", "")
        if last_msg:
            rec["response"] = str(last_msg)
        # Capture transcript_path if available
        tp = data.get("transcript_path", "")
        if tp:
            rec["tp"] = str(tp)
        return rec

    elif hook_type == "subagent_start":
        return {
            "e": "agent_start",
            "agent": inp.get("subagent_type", data.get("subagent_type", "?")),
            "agent_id": data.get("agent_id", data.get("session_id", "")),
        }

    elif hook_type == "subagent_stop":
        rec = {
            "e": "agent_stop",
            "agent": data.get("subagent_type", inp.get("subagent_type", "?")),
            "agent_id": data.get("agent_id", data.get("session_id", "")),
        }
        # Capture agent response
        resp = data.get("response", data.get("tool_response", ""))
        if resp:
            rec["response"] = str(resp)
        # Capture agent transcript path
        atp = data.get("transcript_path", "")
        if atp:
            rec["atp"] = str(atp)
        return rec

    return None


def main() -> None:
    # Skip if this is the observer's own process
    if os.environ.get("CL_OBSERVER") == "1":
        sys.exit(0)

    if len(sys.argv) < 2:
        sys.exit(1)

    hook_type = sys.argv[1]
    config = load_config()

    # Read stdin (hook payload)
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, Exception):
        data = {}

    rec = build_record(hook_type, data, config)
    if rec is None:
        sys.exit(0)

    # Common fields
    session_id = data.get("session_id", os.environ.get("CLAUDE_SESSION_ID", "unknown"))
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec["v"] = 4
    rec["ts"] = ts
    rec["sid"] = session_id

    # Add transcript_path to turn events if available
    if hook_type == "user_prompt":
        tp = data.get("transcript_path", "")
        if tp:
            rec["tp"] = str(tp)

    # Ensure data dir exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Rotate if needed
    rotate_if_needed(config)

    # Append to JSONL (atomic write with newline)
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
    with open(JSONL, "a") as f:
        f.write(line)


if __name__ == "__main__":
    main()
