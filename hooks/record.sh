#!/usr/bin/env bash
# Continuous Learning v3 - Hook recorder
# Pure recording: appends structured JSONL events to turns.jsonl
# No analysis, no process management. Observer is completely separate.
#
# Usage (registered in ~/.claude/settings.json):
#   record.sh user_prompt   — UserPromptSubmit hook
#   record.sh pre_tool      — PreToolUse hook (all tools)
#   record.sh bash_result   — PostToolUse hook (Bash only)
#   record.sh tool_fail     — PostToolUseFailure hook (all tools)
#   record.sh stop          — Stop hook

set -euo pipefail

# Skip recording if this is the observer's own claude process
[ "${CL_OBSERVER:-}" = "1" ] && exit 0

HOOK_TYPE="${1:-}"
[ -z "$HOOK_TYPE" ] && exit 1

CL_DIR="$HOME/.claude/continual-learning"
DATA_DIR="$CL_DIR/data"
JSONL="$DATA_DIR/turns.jsonl"
CONFIG="$CL_DIR/config.json"

# Ensure data dir exists
mkdir -p "$DATA_DIR"

# Read stdin (hook payload from Claude Code)
INPUT="$(cat)"

# Invoke embedded Python for structured JSON processing
python3 -c '
import json, sys, os
from datetime import datetime, timezone

hook_type = sys.argv[1]
config_path = sys.argv[2]
jsonl_path = sys.argv[3]

# Parse hook payload
try:
    data = json.loads(sys.stdin.read()) if sys.stdin.readable() else {}
except (json.JSONDecodeError, Exception):
    data = {}

# Load config for tool recording rules
try:
    with open(config_path) as f:
        config = json.load(f)
except Exception:
    config = {}

tool_cfg = config.get("tool_recording", {})
ignore_tools = set(tool_cfg.get("ignore", []))
overrides = tool_cfg.get("overrides", {})
default_cfg = tool_cfg.get("default", {})

# Common fields
session_id = data.get("session_id", os.environ.get("CLAUDE_SESSION_ID", "unknown"))
ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

rec = None
tool = data.get("tool_name", "")
inp = data.get("tool_input", {})

if hook_type == "user_prompt":
    prompt = data.get("prompt", "")
    if not prompt:
        # Some hook formats use different field names
        prompt = data.get("message", data.get("content", ""))
    rec = {
        "e": "turn",
        "prompt": str(prompt)[:3000],
        "cwd": data.get("cwd", os.getcwd())
    }

elif hook_type == "pre_tool":
    if not tool or tool in ignore_tools:
        sys.exit(0)

    tool_override = overrides.get(tool, {})
    input_mode = tool_override.get("input", default_cfg.get("input", "target_only"))

    if input_mode == "delegate":
        # Task tool: record subagent delegation
        rec = {
            "e": "delegate",
            "agent": inp.get("subagent_type", "?"),
            "agent_prompt": str(inp.get("prompt", ""))[:2000]
        }
    elif input_mode == "skill":
        # Skill tool: record skill invocation
        rec = {
            "e": "skill",
            "skill": inp.get("skill", "?"),
            "args": str(inp.get("args", ""))[:500]
        }
    elif input_mode == "detailed":
        # Detailed input tools: record specified fields
        rec = {"e": "tool", "tool": tool}
        fields = tool_override.get("fields", [])
        for field in fields:
            val = inp.get(field, "")
            if val:
                rec[field] = str(val)[:500]
    else:
        # Default: target_only - just record path/pattern/url
        rec = {"e": "tool", "tool": tool}
        target_fields = default_cfg.get("target_fields", ["file_path", "pattern", "url"])
        for field in target_fields:
            val = inp.get(field, "")
            if val:
                rec["target"] = str(val)[:300]
                break

elif hook_type == "bash_result":
    # PostToolUse for Bash only - record successful result
    resp = data.get("tool_response", "")
    cmd = str(inp.get("command", data.get("tool_input", {}).get("command", "")))
    rec = {
        "e": "bash_ok",
        "cmd": cmd[:200],
        "out": str(resp)[:500]
    }

elif hook_type == "tool_fail":
    if tool in ignore_tools:
        sys.exit(0)
    rec = {
        "e": "fail",
        "tool": tool,
        "error": str(data.get("tool_response", data.get("error", "")))[:500]
    }

elif hook_type == "stop":
    rec = {"e": "stop"}

if rec is None:
    sys.exit(0)

# Add common fields
rec["v"] = 3
rec["ts"] = ts
rec["sid"] = session_id

# Append to JSONL (atomic write with newline)
line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"

# Check file size limit (default 10MB)
max_size = config.get("data", {}).get("max_file_size_mb", 10) * 1024 * 1024
try:
    current_size = os.path.getsize(jsonl_path)
except OSError:
    current_size = 0

if current_size >= max_size:
    # Rotate: move current file to archive
    # Use try/except to handle race condition when multiple hooks trigger simultaneously
    archive_dir = os.path.join(os.path.dirname(jsonl_path), "turns.archive")
    os.makedirs(archive_dir, exist_ok=True)
    archive_name = datetime.now().strftime("%Y%m%d-%H%M%S") + ".jsonl"
    try:
        os.rename(jsonl_path, os.path.join(archive_dir, archive_name))
    except OSError:
        pass  # Another hook already rotated the file

with open(jsonl_path, "a") as f:
    f.write(line)
' "$HOOK_TYPE" "$CONFIG" "$JSONL" <<< "$INPUT"
