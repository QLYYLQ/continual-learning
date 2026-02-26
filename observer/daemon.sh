#!/usr/bin/env bash
# Continuous Learning v3 - Observer Daemon
#
# Independent daemon that polls for new data and runs the 3-stage
# analysis pipeline. Completely decoupled from hooks.
#
# Usage:
#   daemon.sh start   — Start daemon in background
#   daemon.sh stop    — Stop running daemon
#   daemon.sh status  — Check daemon status
#   daemon.sh run     — Run once (for testing)
#   daemon.sh loop    — Run the poll loop (called by start)

set -euo pipefail

CL_DIR="$HOME/.claude/continual-learning"
DATA_DIR="$CL_DIR/data"
OBSERVER_DIR="$CL_DIR/observer"
JSONL="$DATA_DIR/turns.jsonl"
CONFIG="$CL_DIR/config.json"
PID_FILE="$DATA_DIR/.observer.pid"
LOG_FILE="$DATA_DIR/analysis.log"
LAST_SIZE_FILE="$DATA_DIR/.last_size"
_DAEMON_MODE=0

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
MODEL_EPISODE=$(read_config "observer.model_episode" "haiku")
MODEL_PATTERN=$(read_config "observer.model_pattern" "sonnet")
MODEL_BASH=$(read_config "observer.model_bash_pattern" "$MODEL_PATTERN")
MIN_TURNS=$(read_config "observer.min_turns_to_analyze" "20")
MIN_EPISODES=$(read_config "observer.min_episodes_for_patterns" "5")
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

    if [ "$current_size" -le "$last_size" ]; then
        log "No new data (size: $current_size, last: $last_size)"
        return 0
    fi

    log "New data detected (size: $current_size, last: $last_size)"

    # Stage 1: Build episodes (Python, no LLM)
    log "Stage 1: Building episodes..."
    if python3 "$OBSERVER_DIR/build_episodes.py" \
        --input "$JSONL" \
        --output "$DATA_DIR/episodes.json" 2>&1 | log_pipe; then
        log "Stage 1 complete"
    else
        log "ERROR: Stage 1 failed"
        return 1
    fi

    # Check if we have enough turns
    turn_count=$(python3 -c "
import json
try:
    d = json.load(open('$DATA_DIR/episodes.json'))
    print(d.get('total_turns', 0))
except Exception:
    print(0)
")
    if [ "$turn_count" -lt "$MIN_TURNS" ]; then
        log "Only $turn_count turns (need $MIN_TURNS), skipping LLM stages"
        echo "$current_size" > "$LAST_SIZE_FILE"
        return 0
    fi

    # Stage 2: Episode analysis (LLM)
    # Writes prompt + data to a temp file, then pipes via stdin to avoid ARG_MAX limits.
    local stage2_succeeded=0
    log "Stage 2: Analyzing episodes with $MODEL_EPISODE..."
    local stage2_prompt_file
    stage2_prompt_file="$DATA_DIR/.stage2_prompt"
    {
        cat "$OBSERVER_DIR/episode_prompt.md"
        echo ""
        echo "Here is the input data:"
        echo ""
        cat "$DATA_DIR/episodes.json"
    } > "$stage2_prompt_file"
    local stage2_raw="$DATA_DIR/.stage2_raw_output"
    cat "$stage2_prompt_file" | CL_OBSERVER=1 CLAUDECODE= claude --model "$MODEL_EPISODE" --print \
        >"$stage2_raw" 2>"$DATA_DIR/.stage2_stderr"

    if [ -s "$stage2_raw" ]; then
        python3 -c "
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
" < "$stage2_raw" > "$DATA_DIR/analyzed_episodes.json" 2>>"$DATA_DIR/.stage2_stderr"
        if [ $? -eq 0 ]; then
            log "Stage 2 complete ($(wc -c < "$DATA_DIR/analyzed_episodes.json") bytes written)"
            stage2_succeeded=1
        else
            log "ERROR: Stage 2 - failed to extract valid JSON from LLM output"
            rm -f "$DATA_DIR/analyzed_episodes.json"
        fi
    else
        log "ERROR: Stage 2 - no output from LLM"
        cat "$DATA_DIR/.stage2_stderr" >> "$LOG_FILE" 2>/dev/null
    fi

    # Stage 3: Pattern detection (LLM, incremental mode)
    if [ "$stage2_succeeded" -eq 1 ] && [ -f "$DATA_DIR/analyzed_episodes.json" ]; then
        # Extract only new (unprocessed) episodes
        local new_episodes
        new_episodes=$(python3 "$OBSERVER_DIR/extract_new_episodes.py" \
            --analyzed "$DATA_DIR/analyzed_episodes.json" \
            --state "$DATA_DIR/.stage3_processed" 2>>"$LOG_FILE")

        local new_count total_count
        new_count=$(echo "$new_episodes" | python3 -c "import sys,json; print(json.load(sys.stdin).get('new_episode_count',0))")
        total_count=$(echo "$new_episodes" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_episode_count',0))")

        if [ "$new_count" -eq 0 ]; then
            log "No new episodes for Stage 3 (total: $total_count already processed)"
        elif [ "$total_count" -lt "$MIN_EPISODES" ]; then
            log "Only $total_count total episodes (need $MIN_EPISODES), skipping Stage 3"
        else
            log "Stage 3: Detecting patterns with $MODEL_PATTERN ($new_count new episodes, $total_count total)..."
            # Write incremental episodes to file for LLM to read
            echo "$new_episodes" > "$DATA_DIR/.stage3_incremental_input.json"
            # Ensure directories exist
            mkdir -p "$CL_DIR/instincts/personal"

            # Create staging directory for LLM to write to
            local staging_dir
            staging_dir=$(mktemp -d "/tmp/cl_stage3_XXXXXX")

            local stage3_prompt
            stage3_prompt="$(cat "$OBSERVER_DIR/pattern_prompt.md")

Incremental episodes file: $DATA_DIR/.stage3_incremental_input.json
Existing instincts directory: $CL_DIR/instincts/personal/
Staging directory (write all output files here): $staging_dir/"

            echo "$stage3_prompt" | CL_OBSERVER=1 CLAUDECODE= claude --model "$MODEL_PATTERN" --print \
                --allowedTools "Read,Write,Glob" \
                >"$DATA_DIR/.stage3_raw_output" 2>"$DATA_DIR/.stage3_stderr"

            if [ -s "$DATA_DIR/.stage3_raw_output" ]; then

                # Move staged files to their final destinations
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
                    # Find and remove instinct file matching this id
                    for inst_file in "$CL_DIR/instincts/personal"/*.yaml; do
                        [ -f "$inst_file" ] || continue
                        if grep -q "^id: $del_id$" "$inst_file" 2>/dev/null; then
                            rm -f "$inst_file"
                            deleted=$((deleted + 1))
                            log "  Deleted instinct: $del_id"
                        fi
                    done
                done

                # Move _learned.md to rules/
                if [ -f "$staging_dir/_learned.md" ]; then
                    cp "$staging_dir/_learned.md" "$HOME/.claude/rules/learned.md"
                    log "  Updated learned.md"
                fi

                # Move _prompt_*.md to prompts/
                for f in "$staging_dir"/_prompt_*.md; do
                    [ -f "$f" ] || continue
                    local agent_name
                    agent_name=$(basename "$f" | sed 's/^_prompt_//;s/\.md$//')
                    cp "$f" "$CL_DIR/prompts/$agent_name.md"
                    log "  Updated prompt: $agent_name"
                done

                # Sync bash_insights.md if any bash_pattern instincts were written
                if ls "$staging_dir"/bash_pattern_*.yaml 1>/dev/null 2>&1 || \
                   ls "$CL_DIR/instincts/personal"/bash_pattern_*.yaml 1>/dev/null 2>&1; then
                    python3 "$CL_DIR/cli/instinct_cli.py" bash-insight sync 2>>"$LOG_FILE" || true
                fi

                # Clean up staging dir
                rm -rf "$staging_dir"

                log "  Staged files applied: $moved moved, $deleted deleted"

                # Mark episodes as processed after successful Stage 3
                python3 "$OBSERVER_DIR/extract_new_episodes.py" \
                    --analyzed "$DATA_DIR/analyzed_episodes.json" \
                    --state "$DATA_DIR/.stage3_processed" \
                    --mark-done 2>>"$LOG_FILE"
                log "Stage 3 complete"
            else
                log "ERROR: Stage 3 - no output from LLM"
                cat "$DATA_DIR/.stage3_stderr" >> "$LOG_FILE" 2>/dev/null
                rm -rf "$staging_dir"
            fi
        fi
    else
        log "Stage 2 did not produce fresh data, skipping Stage 3"
    fi

    # Stage 3b: Bash pattern analysis (LLM with Bash tool access)
    log "Stage 3b: Extracting bash contexts..."

    # Step 1: Run extraction script → JSONL
    local contexts_file="$DATA_DIR/.stage3b_contexts.jsonl"
    python3 "$OBSERVER_DIR/extract_bash_contexts.py" \
        --input "$JSONL" \
        --state "$DATA_DIR/.stage3b_processed" \
        --output "$contexts_file" 2>>"$LOG_FILE"

    # Step 2: Check if we have enough data
    local bash_context_count
    bash_context_count=$(wc -l < "$contexts_file" 2>/dev/null || echo 0)

    if [ "$bash_context_count" -eq 0 ]; then
        log "No new bash contexts for Stage 3b"
    elif [ "$bash_context_count" -lt "$MIN_BASH_EVENTS" ]; then
        log "Only $bash_context_count bash contexts (need $MIN_BASH_EVENTS), skipping Stage 3b"
    else
        log "Stage 3b: Analyzing $bash_context_count bash contexts with $MODEL_BASH..."

        # Step 3: Create staging directory
        local staging_dir_3b
        staging_dir_3b=$(mktemp -d "/tmp/cl_stage3b_XXXXXX")

        # Step 4: Build prompt file (avoid ARG_MAX — write to file, pipe via stdin)
        local stage3b_prompt_file="$DATA_DIR/.stage3b_prompt"
        sed -e "s|{data_file}|$contexts_file|g" \
            -e "s|{instincts_dir}|$CL_DIR/instincts/personal/|g" \
            -e "s|{staging_dir}|$staging_dir_3b|g" \
            -e "s|{turns_file}|$JSONL|g" \
            "$OBSERVER_DIR/bash_pattern_prompt.md" > "$stage3b_prompt_file"

        # Step 5: Run LLM with Bash tool access
        cat "$stage3b_prompt_file" | \
            CL_OBSERVER=1 CLAUDECODE= claude --model "$MODEL_BASH" --print \
            --allowedTools "Bash,Read,Write,Glob" \
            >"$DATA_DIR/.stage3b_raw_output" 2>"$DATA_DIR/.stage3b_stderr"

        if [ -s "$DATA_DIR/.stage3b_raw_output" ]; then

            # Step 6: Move staged instinct files (same logic as Stage 3)
            local moved_3b=0
            for f in "$staging_dir_3b"/*.yaml; do
                [ -f "$f" ] || continue
                cp "$f" "$CL_DIR/instincts/personal/$(basename "$f")"
                moved_3b=$((moved_3b + 1))
            done

            # Handle deletions
            for f in "$staging_dir_3b"/_delete_*; do
                [ -f "$f" ] || continue
                local del_id
                del_id=$(cat "$f" | tr -d '[:space:]')
                for inst_file in "$CL_DIR/instincts/personal"/*.yaml; do
                    [ -f "$inst_file" ] || continue
                    if grep -q "^id: $del_id$" "$inst_file" 2>/dev/null; then
                        rm -f "$inst_file"
                        log "  Deleted instinct: $del_id"
                    fi
                done
            done

            rm -rf "$staging_dir_3b"

            # Step 7: Sync bash_insights.md
            python3 "$CL_DIR/cli/instinct_cli.py" bash-insight sync 2>>"$LOG_FILE" || true

            # Step 8: Mark sessions as processed
            python3 "$OBSERVER_DIR/extract_bash_contexts.py" \
                --input "$JSONL" \
                --state "$DATA_DIR/.stage3b_processed" \
                --mark-done 2>>"$LOG_FILE"

            log "Stage 3b complete ($moved_3b instincts written)"
        else
            log "ERROR: Stage 3b - no output from LLM"
            cat "$DATA_DIR/.stage3b_stderr" >> "$LOG_FILE" 2>/dev/null
            rm -rf "$staging_dir_3b"
        fi
    fi

    local retry_file="$DATA_DIR/.stage2_retry_count"
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
        mkdir -p "$DATA_DIR"
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
        if [ -f "$PID_FILE" ]; then
            pid=$(cat "$PID_FILE")
            if kill -0 "$pid" 2>/dev/null; then
                echo "Observer is running (PID: $pid)"
                echo "Interval: ${INTERVAL}s"
                echo "Log: $LOG_FILE"
                if [ -f "$LAST_SIZE_FILE" ]; then
                    echo "Last analyzed size: $(cat "$LAST_SIZE_FILE") bytes"
                fi
                if [ -f "$DATA_DIR/episodes.json" ]; then
                    python3 -c "
import json
d = json.load(open('$DATA_DIR/episodes.json'))
print(f\"Episodes data: {d.get('total_turns', 0)} turns, {d.get('total_sessions', 0)} sessions\")
" 2>/dev/null || true
                fi
                if [ -f "$DATA_DIR/analyzed_episodes.json" ]; then
                    python3 -c "
import json
d = json.load(open('$DATA_DIR/analyzed_episodes.json'))
ep_count = sum(len(s.get('episodes', [])) for s in d.get('sessions', []))
print(f'Analyzed: {ep_count} episodes')
" 2>/dev/null || true
                fi
                exit 0
            else
                rm -f "$PID_FILE"
                echo "Observer is not running (stale PID file removed)"
                exit 1
            fi
        else
            echo "Observer is not running"
            exit 1
        fi
        ;;

    run)
        run_once
        ;;

    loop)
        run_loop
        ;;

    *)
        echo "Usage: $0 {start|stop|status|run}"
        echo ""
        echo "  start   Start observer daemon in background"
        echo "  stop    Stop running observer daemon"
        echo "  status  Check observer daemon status"
        echo "  run     Run one analysis cycle (for testing)"
        exit 1
        ;;
esac
