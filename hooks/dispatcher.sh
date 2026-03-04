#!/usr/bin/env bash
# Continuous Learning v4 - Unified Hook Dispatcher
#
# Single entry-point for ALL CL hooks. Routes events to sub-hooks
# based on routing.json configuration.
#
# Uses a temporary file for stdin to avoid bash variable size limits
# when passing large payloads to sub-hooks.
#
# Usage (registered in hooks.json):
#   dispatcher.sh pre_tool       — PreToolUse hook
#   dispatcher.sh post_tool      — PostToolUse hook
#   dispatcher.sh user_prompt    — UserPromptSubmit hook
#   dispatcher.sh tool_fail      — PostToolUseFailure hook
#   dispatcher.sh stop           — Stop hook
#   dispatcher.sh subagent_start — SubagentStart hook
#   dispatcher.sh subagent_stop  — SubagentStop hook
#
# Sub-hooks:
#   record    → record.py (always exit 0, recording only)
#   intercept → intercept.py (may exit 2 to block Bash commands)

set -euo pipefail

# Skip if this is the observer's own claude process
[ "${CL_OBSERVER:-}" = "1" ] && exit 0

EVENT_TYPE="${1:-}"
[ -z "$EVENT_TYPE" ] && exit 1

CL_DIR="$HOME/.claude/continual-learning"
HOOKS_DIR="$CL_DIR/hooks"
HOOKS_CONFIG="$HOOKS_DIR/routing.json"

# Read stdin to a temporary file to avoid bash variable size limits
INPUT_FILE=$(mktemp)
trap 'rm -f "$INPUT_FILE"' EXIT
cat > "$INPUT_FILE"

# Extract tool_name from JSON payload
TOOL_NAME=$(python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get("tool_name", ""))
except Exception:
    print("")
' < "$INPUT_FILE" 2>/dev/null || echo "")

# Determine which sub-hooks to run by reading routing.json
SUB_HOOKS=$(python3 -c '
import json, sys

event_type = sys.argv[1]
tool_name = sys.argv[2]
config_path = sys.argv[3]

try:
    with open(config_path) as f:
        config = json.load(f)
except Exception:
    # Fallback: just run record for everything
    print("record")
    sys.exit(0)

event_cfg = config.get(event_type, {})

# Collect sub-hooks: wildcard first, then tool-specific
hooks = set()
wildcard = event_cfg.get("*", [])
for h in wildcard:
    hooks.add(h)

if tool_name:
    tool_specific = event_cfg.get(tool_name, [])
    for h in tool_specific:
        hooks.add(h)

# Output in deterministic order: record always first, then intercept
ordered = []
if "record" in hooks:
    ordered.append("record")
    hooks.discard("record")
for h in sorted(hooks):
    ordered.append(h)

print("\n".join(ordered))
' "$EVENT_TYPE" "$TOOL_NAME" "$HOOKS_CONFIG" 2>/dev/null || echo "record")

# Map hook types to appropriate record.py event names
map_event_to_record_type() {
    local event="$1"
    case "$event" in
        pre_tool)       echo "pre_tool" ;;
        post_tool)      echo "bash_result" ;;
        user_prompt)    echo "user_prompt" ;;
        tool_fail)      echo "tool_fail" ;;
        stop)           echo "stop" ;;
        subagent_start) echo "subagent_start" ;;
        subagent_stop)  echo "subagent_stop" ;;
        *)              echo "$event" ;;
    esac
}

RECORD_TYPE=$(map_event_to_record_type "$EVENT_TYPE")

# Run each sub-hook sequentially
FINAL_EXIT=0
BLOCK_MSG=""

while IFS= read -r hook; do
    [ -z "$hook" ] && continue

    case "$hook" in
        record)
            # Run record.py — always exit 0
            python3 "$HOOKS_DIR/record.py" "$RECORD_TYPE" < "$INPUT_FILE" 2>/dev/null || true
            ;;
        intercept)
            # Run intercept.py — may exit 2 to block
            INTERCEPT_STDERR=$(python3 "$HOOKS_DIR/intercept.py" < "$INPUT_FILE" 2>&1 1>/dev/null) || {
                EXIT_CODE=$?
                if [ "$EXIT_CODE" -eq 2 ]; then
                    FINAL_EXIT=2
                    BLOCK_MSG="$INTERCEPT_STDERR"
                fi
            }
            ;;
        trigger)
            # Run trigger evaluator — may run Stage 1 and queue downstream stages
            # Map event type to capitalized form matching config (stop -> Stop)
            local trigger_event
            trigger_event="$(echo "${EVENT_TYPE:0:1}" | tr '[:lower:]' '[:upper:]')${EVENT_TYPE:1}"
            python3 "$HOOKS_DIR/trigger_evaluator.py" on_hook "$trigger_event" 2>>"$CL_DIR/data/log/trigger.log" || true
            ;;
        *)
            # Unknown sub-hook, skip
            ;;
    esac
done <<< "$SUB_HOOKS"

# If any sub-hook blocked (exit 2), propagate
if [ "$FINAL_EXIT" -eq 2 ]; then
    echo "$BLOCK_MSG" >&2
    exit 2
fi

exit 0
