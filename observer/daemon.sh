#!/usr/bin/env bash
# Continuous Learning v3 - Observer Daemon (Task-Centric Pipeline)
#
# Independent daemon that polls for new data and runs the 3-stage
# task-centric analysis pipeline. Completely decoupled from hooks.
#
# Data directory layout:
#   data/
#   ├── turns.jsonl              # Raw event stream (append-only)
#   ├── turns.archive/           # Rotated JSONL archives
#   ├── sessions/                # Stage 1 per-session JSON + _index.json
#   ├── task_registry.json       # Core persistent state
#   ├── state/                   # Lightweight persistent state
#   │   ├── .last_size           # JSONL file size checkpoint
#   │   ├── .stage2_cursor.json  # Which sessions Stage 2 has processed
#   │   ├── .stage2_retry_count  # Stage 2 failure retry counter
#   │   └── .observer.pid        # Daemon PID file
#   ├── cache/                   # Intermediate artifacts (rebuildable)
#   │   ├── stage2_manifest.json
#   │   ├── stage2_ops.json
#   │   ├── stage2_prompt
#   │   ├── stage2_raw_output
#   │   ├── stage3_bundle.json
#   │   ├── stage3_raw_output
#   │   ├── stage3b_prompt
#   │   ├── stage3b_raw_output
#   │   └── stage3b_task_contexts.json
#   └── log/                     # Logs and stderr captures
#       ├── analysis.log
#       ├── stage2_stderr
#       ├── stage3_stderr
#       └── stage3b_stderr
#
# Usage:
#   daemon.sh start   — Start daemon in background
#   daemon.sh stop    — Stop running daemon
#   daemon.sh status  — Check daemon status
#   daemon.sh run     — Run once (for testing)
#   daemon.sh batch --start-time ISO --end-time ISO — Process a time window
#   daemon.sh loop    — Run the poll loop (called by start)

set -euo pipefail

CL_DIR="$HOME/.claude/continual-learning"
DATA_DIR="$CL_DIR/data"
SESSIONS_DIR="$DATA_DIR/sessions"
STATE_DIR="$DATA_DIR/state"
CACHE_DIR="$DATA_DIR/cache"
LOG_DIR="$DATA_DIR/log"
OBSERVER_DIR="$CL_DIR/observer"
JSONL="$DATA_DIR/turns.jsonl"
CONFIG="$CL_DIR/config.json"

# Persistent state files
PID_FILE="$STATE_DIR/.observer.pid"
LAST_SIZE_FILE="$STATE_DIR/.last_size"
REGISTRY="$DATA_DIR/task_registry.json"
INDEX="$SESSIONS_DIR/_index.json"
CURSOR="$STATE_DIR/.stage2_cursor.json"

# Log files
LOG_FILE="$LOG_DIR/analysis.log"

_DAEMON_MODE=0

# Batch mode time filters (set by batch command)
BATCH_START_TIME=""
BATCH_END_TIME=""

# Ensure directory structure exists
mkdir -p "$STATE_DIR" "$CACHE_DIR" "$LOG_DIR" "$SESSIONS_DIR" "$CL_DIR/prompts" "$CL_DIR/instincts/personal"

# Load config
read_config() {
    python3 -c "
import json, sys
try:
    with open('$CONFIG') as f:
        cfg = json.load(f)
    key = sys.argv[1]
    parts = key.split('.')
    val = cfg
    for p in parts:
        val = val[p]
    print(val)
except Exception:
    print(sys.argv[2] if len(sys.argv) > 2 else '')
" "$1" "${2:-}"
}

INTERVAL=$(read_config "observer.interval_seconds" "600")
MODEL_TASK=$(read_config "observer.model_episode" "haiku")
MODEL_PATTERN=$(read_config "observer.model_pattern" "sonnet")
MODEL_BASH=$(read_config "observer.model_bash_pattern" "$MODEL_PATTERN")
MIN_TURNS=$(read_config "observer.min_turns_to_analyze" "14")
MIN_DIRTY_TASKS=$(read_config "observer.min_dirty_tasks_for_stage3" "1")
MIN_BASH_EVENTS=$(read_config "observer.min_bash_events" "10")

log() {
    local msg="[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
    if [ "$_DAEMON_MODE" -eq 1 ]; then
        echo "$msg"
    else
        echo "$msg" | tee -a "$LOG_FILE"
    fi
}

log_pipe() {
    if [ "$_DAEMON_MODE" -eq 1 ]; then cat; else tee -a "$LOG_FILE"; fi
}

# Apply staged files from a staging directory to their final destinations
# Reused by both Stage 3 and Stage 3b
apply_staging_dir() {
    local staging_dir="$1"
    local stage_name="$2"
    local moved=0 deleted=0

    # Move instinct YAML files to personal/
    for f in "$staging_dir"/*.yaml; do
        [ -f "$f" ] || continue
        local basename
        basename=$(basename "$f")
        cp "$f" "$CL_DIR/instincts/personal/$basename"
        moved=$((moved + 1))
    done

    # Handle deletions (_delete_* files)
    for f in "$staging_dir"/_delete_*; do
        [ -f "$f" ] || continue
        local del_id
        del_id=$(cat "$f" | tr -d '[:space:]')
        for inst_file in "$CL_DIR/instincts/personal"/*.yaml; do
            [ -f "$inst_file" ] || continue
            if grep -q "^id: $del_id$" "$inst_file" 2>/dev/null; then
                rm -f "$inst_file"
                deleted=$((deleted + 1))
                log "  [$stage_name] Deleted instinct: $del_id"
            fi
        done
    done

    # Move _learned.md to rules/
    if [ -f "$staging_dir/_learned.md" ]; then
        cp "$staging_dir/_learned.md" "$HOME/.claude/rules/learned.md"
        log "  [$stage_name] Updated learned.md"
    fi

    # Move _prompt_*.md to prompts/
    for f in "$staging_dir"/_prompt_*.md; do
        [ -f "$f" ] || continue
        local agent_name
        agent_name=$(basename "$f" | sed 's/^_prompt_//;s/\.md$//')
        cp "$f" "$CL_DIR/prompts/$agent_name.md"
        log "  [$stage_name] Updated prompt: $agent_name"
    done

    # Sync bash_insights.md if any bash_pattern instincts exist
    if ls "$staging_dir"/bash_pattern_*.yaml 1>/dev/null 2>&1 || \
       ls "$CL_DIR/instincts/personal"/bash_pattern_*.yaml 1>/dev/null 2>&1; then
        python3 "$CL_DIR/cli/instinct_cli.py" bash-insight sync 2>>"$LOG_FILE" || true
    fi

    # Clean up staging dir
    rm -rf "$staging_dir"

    log "  [$stage_name] Staged files applied: $moved moved, $deleted deleted"
}

# Run one analysis cycle
run_once() {
    log "Starting analysis cycle"

    # Check if JSONL exists and has data
    if [ ! -f "$JSONL" ]; then
        log "No turns.jsonl found, skipping"
        return 0
    fi

    current_size=$(stat -c%s "$JSONL" 2>/dev/null || stat -f%z "$JSONL" 2>/dev/null || echo 0)
    last_size=$(cat "$LAST_SIZE_FILE" 2>/dev/null || echo 0)

    # In batch mode, skip size guard — incrementality is handled by
    # the Stage 2 cursor and time filters, not file size.
    if [ -z "$BATCH_START_TIME" ]; then
        if [ "$current_size" -le "$last_size" ]; then
            log "No new data (size: $current_size, last: $last_size)"
            return 0
        fi
        log "New data detected (size: $current_size, last: $last_size)"
    else
        log "Batch mode — skipping size guard"
    fi

    # ── Stage 1: Segment sessions (Python, no LLM) ──
    log "Stage 1: Segmenting sessions..."
    if python3 "$OBSERVER_DIR/segment_sessions.py" \
        --input "$JSONL" \
        --outdir "$SESSIONS_DIR" \
        --index "$INDEX" \
        --config "$CONFIG" 2>&1 | log_pipe; then
        log "Stage 1 complete"
    else
        log "ERROR: Stage 1 failed"
        return 1
    fi

    # Check if we have enough turns
    local turn_count
    turn_count=$(python3 -c "
import json
try:
    d = json.load(open('$INDEX'))
    print(d.get('total_turns', 0))
except Exception:
    print(0)
")
    if [ "$turn_count" -lt "$MIN_TURNS" ]; then
        log "Only $turn_count turns (need $MIN_TURNS), skipping LLM stages"
        echo "$current_size" > "$LAST_SIZE_FILE"
        return 0
    fi

    # ── Stage 2: Task classification (LLM) ──
    local stage2_succeeded=0

    # Build time filter args
    local time_args=""
    if [ -n "$BATCH_START_TIME" ]; then
        time_args="$time_args --start-time $BATCH_START_TIME"
    fi
    if [ -n "$BATCH_END_TIME" ]; then
        time_args="$time_args --end-time $BATCH_END_TIME"
    fi

    # Step 2a: Prepare manifest
    local manifest="$CACHE_DIR/stage2_manifest.json"
    local prepare_output
    prepare_output=$(python3 "$OBSERVER_DIR/prepare_stage2.py" \
        --index "$INDEX" \
        --cursor "$CURSOR" \
        --registry "$REGISTRY" \
        --sessions-dir "$SESSIONS_DIR" \
        --manifest "$manifest" \
        $time_args 2>>"$LOG_FILE") || true

    local new_count
    new_count=$(echo "$prepare_output" | grep -oP 'new_count=\K[0-9]+' || echo 0)

    if [ "$new_count" -eq 0 ]; then
        log "Stage 2: No new sessions to classify"
        stage2_succeeded=1
    else
        log "Stage 2: Classifying $new_count sessions into tasks with $MODEL_TASK..."

        # Step 2b: Build prompt with manifest path
        local stage2_prompt_file="$CACHE_DIR/stage2_prompt"
        {
            cat "$OBSERVER_DIR/task_prompt.md"
            echo ""
            echo "Manifest file: $manifest"
        } > "$stage2_prompt_file"

        # Step 2c: Run LLM
        local stage2_raw="$CACHE_DIR/stage2_raw_output"
        cat "$stage2_prompt_file" | CL_OBSERVER=1 CLAUDECODE= claude --model "$MODEL_TASK" --print \
            --allowedTools "Read" \
            >"$stage2_raw" 2>"$LOG_DIR/stage2_stderr"

        if [ -s "$stage2_raw" ]; then
            # Step 2d: Extract JSON operations from LLM output
            local ops_file="$CACHE_DIR/stage2_ops.json"
            if python3 -c "
import sys, json
text = sys.stdin.read()
start = text.find('{')
end = text.rfind('}')
if start >= 0 and end > start:
    candidate = text[start:end+1]
    try:
        obj = json.loads(candidate)
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    except json.JSONDecodeError:
        print('INVALID_JSON', file=sys.stderr)
        sys.exit(1)
else:
    print('NO_JSON_FOUND', file=sys.stderr)
    sys.exit(1)
" < "$stage2_raw" > "$ops_file" 2>>"$LOG_DIR/stage2_stderr"; then
                # Step 2e: Apply operations to task registry
                python3 "$OBSERVER_DIR/apply_stage2.py" \
                    --ops "$ops_file" \
                    --registry "$REGISTRY" \
                    --cursor "$CURSOR" \
                    --manifest "$manifest" 2>>"$LOG_FILE"

                log "Stage 2 complete"
                stage2_succeeded=1
            else
                log "ERROR: Stage 2 - failed to extract valid JSON from LLM output"
            fi
        else
            log "ERROR: Stage 2 - no output from LLM"
            cat "$LOG_DIR/stage2_stderr" >> "$LOG_FILE" 2>/dev/null
        fi
    fi

    # ── Stage 3: Task-centric pattern analysis (LLM) ──
    local stage3_succeeded=0
    if [ "$stage2_succeeded" -eq 1 ]; then
        # Step 3a: Prepare bundle of dirty tasks
        local bundle="$CACHE_DIR/stage3_bundle.json"
        local prepare3_output
        prepare3_output=$(python3 "$OBSERVER_DIR/prepare_stage3.py" \
            --registry "$REGISTRY" \
            --sessions-dir "$SESSIONS_DIR" \
            --output "$bundle" 2>>"$LOG_FILE") || true

        local dirty_count
        dirty_count=$(echo "$prepare3_output" | grep -oP 'dirty_count=\K[0-9]+' || echo 0)

        if [ "$dirty_count" -eq 0 ]; then
            log "Stage 3: No dirty tasks to analyze"
            stage3_succeeded=1
        elif [ "$dirty_count" -lt "$MIN_DIRTY_TASKS" ]; then
            log "Stage 3: Only $dirty_count dirty tasks (need $MIN_DIRTY_TASKS), skipping"
            stage3_succeeded=1
        else
            log "Stage 3: Analyzing $dirty_count dirty tasks with $MODEL_PATTERN..."
            mkdir -p "$CL_DIR/instincts/personal"

            local staging_dir
            staging_dir=$(mktemp -d "/tmp/cl_stage3_XXXXXX")

            local stage3_prompt
            stage3_prompt="$(cat "$OBSERVER_DIR/task_analysis_prompt.md")

Task bundle file: $bundle
Existing instincts directory: $CL_DIR/instincts/personal/
Staging directory (write all output files here): $staging_dir/"

            echo "$stage3_prompt" | CL_OBSERVER=1 CLAUDECODE= claude --model "$MODEL_PATTERN" --print \
                --allowedTools "Read,Write,Glob" \
                >"$CACHE_DIR/stage3_raw_output" 2>"$LOG_DIR/stage3_stderr"

            if [ -s "$CACHE_DIR/stage3_raw_output" ]; then
                apply_staging_dir "$staging_dir" "Stage3"
                stage3_succeeded=1
                log "Stage 3 complete"
            else
                log "ERROR: Stage 3 - no output from LLM"
                cat "$LOG_DIR/stage3_stderr" >> "$LOG_FILE" 2>/dev/null
                rm -rf "$staging_dir"
            fi
        fi
    else
        log "Stage 2 failed, skipping Stage 3"
    fi

    # ── Stage 3b: Task-driven bash pattern analysis (LLM with Bash tool) ──
    local stage3b_succeeded=0
    if [ "$stage2_succeeded" -eq 1 ]; then
        log "Stage 3b: Extracting task bash contexts..."

        local contexts_file="$CACHE_DIR/stage3b_task_contexts.json"
        local bash_output
        bash_output=$(python3 "$OBSERVER_DIR/extract_bash_contexts.py" \
            --task-registry "$REGISTRY" \
            --sessions-dir "$SESSIONS_DIR" \
            --output "$contexts_file" 2>>"$LOG_FILE") || true

        local bash_context_count
        bash_context_count=$(echo "$bash_output" | grep -oP 'bash_context_count=\K[0-9]+' || echo 0)

        if [ "$bash_context_count" -eq 0 ]; then
            log "Stage 3b: No bash contexts for dirty tasks"
            stage3b_succeeded=1
        elif [ "$bash_context_count" -lt "$MIN_BASH_EVENTS" ]; then
            log "Stage 3b: Only $bash_context_count bash contexts (need $MIN_BASH_EVENTS), skipping"
            stage3b_succeeded=1
        else
            log "Stage 3b: Analyzing $bash_context_count bash contexts with $MODEL_BASH..."

            local staging_dir_3b
            staging_dir_3b=$(mktemp -d "/tmp/cl_stage3b_XXXXXX")

            local stage3b_prompt_file="$CACHE_DIR/stage3b_prompt"
            sed -e "s|{data_file}|$contexts_file|g" \
                -e "s|{instincts_dir}|$CL_DIR/instincts/personal/|g" \
                -e "s|{staging_dir}|$staging_dir_3b|g" \
                "$OBSERVER_DIR/bash_pattern_prompt.md" > "$stage3b_prompt_file"

            cat "$stage3b_prompt_file" | \
                CL_OBSERVER=1 CLAUDECODE= claude --model "$MODEL_BASH" --print \
                --allowedTools "Bash,Read,Write,Glob" \
                >"$CACHE_DIR/stage3b_raw_output" 2>"$LOG_DIR/stage3b_stderr"

            if [ -s "$CACHE_DIR/stage3b_raw_output" ]; then
                apply_staging_dir "$staging_dir_3b" "Stage3b"
                stage3b_succeeded=1
                log "Stage 3b complete"
            else
                log "ERROR: Stage 3b - no output from LLM"
                cat "$LOG_DIR/stage3b_stderr" >> "$LOG_FILE" 2>/dev/null
                rm -rf "$staging_dir_3b"
            fi
        fi
    else
        log "Stage 2 failed, skipping Stage 3b"
    fi

    # ── Clear dirty_task_ids after both Stage 3 and 3b succeed ──
    if [ "$stage3_succeeded" -eq 1 ] && [ "$stage3b_succeeded" -eq 1 ]; then
        if [ -f "$REGISTRY" ]; then
            python3 -c "
import json, os
path = '$REGISTRY'
try:
    with open(path) as f:
        reg = json.load(f)
    reg['dirty_task_ids'] = []
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(reg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
except Exception:
    pass
" 2>>"$LOG_FILE"
            log "Cleared dirty_task_ids in registry"
        fi
    fi

    # ── Checkpoint ──
    local retry_file="$STATE_DIR/.stage2_retry_count"
    if [ "$stage2_succeeded" -eq 1 ]; then
        echo "$current_size" > "$LAST_SIZE_FILE"
        echo 0 > "$retry_file"
        log "Analysis cycle complete"
    else
        local retry_count
        retry_count=$(cat "$retry_file" 2>/dev/null || echo 0)
        retry_count=$((retry_count + 1))
        echo "$retry_count" > "$retry_file"
        if [ "$retry_count" -ge 3 ]; then
            echo "$current_size" > "$LAST_SIZE_FILE"
            echo 0 > "$retry_file"
            log "Analysis cycle complete (gave up after $retry_count failures, advancing size checkpoint)"
        else
            log "Analysis cycle complete (Stage 2 failed, retry $retry_count/3, size checkpoint NOT saved)"
        fi
    fi
}

# Main poll loop
run_loop() {
    _DAEMON_MODE=1
    log "Observer daemon starting (interval: ${INTERVAL}s)"

    while true; do
        sleep "$INTERVAL"
        run_once || true
    done
}

# Command dispatch
case "${1:-}" in
    start)
        if [ -f "$PID_FILE" ]; then
            old_pid=$(cat "$PID_FILE")
            if kill -0 "$old_pid" 2>/dev/null; then
                echo "Observer already running (PID: $old_pid)"
                exit 0
            fi
            rm -f "$PID_FILE"
        fi
        mkdir -p "$DATA_DIR" "$STATE_DIR" "$CACHE_DIR" "$LOG_DIR"
        nohup "$0" loop >> "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        echo "Observer started (PID: $!)"
        echo "Log: $LOG_FILE"
        ;;

    stop)
        if [ -f "$PID_FILE" ]; then
            pid=$(cat "$PID_FILE")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid"
                rm -f "$PID_FILE"
                echo "Observer stopped (PID: $pid)"
            else
                rm -f "$PID_FILE"
                echo "Observer was not running (stale PID file removed)"
            fi
        else
            echo "Observer is not running"
        fi
        ;;

    status)
        echo "=== Observer Status ==="
        if [ -f "$PID_FILE" ]; then
            pid=$(cat "$PID_FILE")
            if kill -0 "$pid" 2>/dev/null; then
                echo "Daemon: running (PID: $pid)"
            else
                rm -f "$PID_FILE"
                echo "Daemon: not running (stale PID removed)"
            fi
        else
            echo "Daemon: not running"
        fi
        echo "Interval: ${INTERVAL}s"
        echo "Log: $LOG_FILE"
        if [ -f "$LAST_SIZE_FILE" ]; then
            echo "Last analyzed size: $(cat "$LAST_SIZE_FILE") bytes"
        fi
        # Session index
        if [ -f "$INDEX" ]; then
            python3 -c "
import json
d = json.load(open('$INDEX'))
print(f\"Sessions: {d.get('total_sessions', 0)} sessions, {d.get('total_turns', 0)} turns\")
" 2>/dev/null || true
        fi
        # Task registry
        if [ -f "$REGISTRY" ]; then
            python3 -c "
import json
d = json.load(open('$REGISTRY'))
tasks = d.get('tasks', {})
dirty = d.get('dirty_task_ids', [])
active = sum(1 for t in tasks.values() if t.get('status') == 'active')
completed = sum(1 for t in tasks.values() if t.get('status') == 'completed')
non_tasks = len(d.get('non_tasks', []))
print(f'Tasks: {len(tasks)} total ({active} active, {completed} completed), {non_tasks} non-tasks')
if dirty:
    print(f'Dirty tasks: {len(dirty)} ({", ".join(dirty[:5])}{"..." if len(dirty) > 5 else ""})')
" 2>/dev/null || true
        fi
        # Instincts
        instinct_count=$(ls "$CL_DIR/instincts/personal"/*.yaml 2>/dev/null | wc -l || echo 0)
        echo "Instincts: $instinct_count personal instincts"
        ;;

    run)
        run_once
        ;;

    batch)
        # Parse batch arguments
        shift
        while [ $# -gt 0 ]; do
            case "$1" in
                --start-time)
                    BATCH_START_TIME="$2"
                    shift 2
                    ;;
                --end-time)
                    BATCH_END_TIME="$2"
                    shift 2
                    ;;
                *)
                    echo "Unknown batch arg: $1" >&2
                    exit 1
                    ;;
            esac
        done
        if [ -z "$BATCH_START_TIME" ] || [ -z "$BATCH_END_TIME" ]; then
            echo "Usage: $0 batch --start-time ISO --end-time ISO" >&2
            exit 1
        fi
        log "Batch mode: $BATCH_START_TIME to $BATCH_END_TIME"
        run_once
        ;;

    loop)
        run_loop
        ;;

    *)
        echo "Usage: $0 {start|stop|status|run|batch}"
        echo ""
        echo "  start   Start observer daemon in background"
        echo "  stop    Stop running observer daemon"
        echo "  status  Check observer daemon status"
        echo "  run     Run one analysis cycle (for testing)"
        echo "  batch   Process a time window (--start-time ISO --end-time ISO)"
        exit 1
        ;;
esac
