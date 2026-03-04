#!/usr/bin/env bash
# Continuous Learning v4 - Observer Daemon (Event-Driven Pipeline)
#
# Each stage has configurable triggers defined in config.json:
#   - Stage 1: Runs on Stop hook (fast, no LLM)
#   - Stage 2: Queued after 3 Stage 1 runs (LLM)
#   - Stage 3/3b: Queued after Stage 2 completes (LLM)
#
# The daemon polls for queued stages and processes them. Trigger
# evaluation is handled by hooks/trigger_evaluator.py.
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
#   │   ├── .observer.pid        # Daemon PID file
#   │   ├── stage_counters.json  # Per-stage run counts
#   │   ├── pending_stages.json  # Queue of stages for daemon
#   │   ├── .stage3_processed_ids   # Dirty task IDs processed by Stage 3
#   │   └── .stage3b_processed_ids  # Dirty task IDs processed by Stage 3b
#   ├── cache/                   # Intermediate artifacts (rebuildable)
#   └── log/                     # Logs and stderr captures
#
# Usage:
#   daemon.sh start   — Start daemon in background
#   daemon.sh stop    — Stop running daemon
#   daemon.sh status  — Check daemon status and stage triggers
#   daemon.sh run     — Run full pipeline (force_all + process queue)
#   daemon.sh stage1  — Run Stage 1 only
#   daemon.sh stage2  — Run Stage 2 only
#   daemon.sh stage3  — Run Stage 3 only
#   daemon.sh stage3b — Run Stage 3b only
#   daemon.sh queue   — Show pending stage queue
#   daemon.sh clear   — Clear dirty tasks processed by both stages
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
QUEUE_FILE="$STATE_DIR/pending_stages.json"
COUNTERS_FILE="$STATE_DIR/stage_counters.json"
STAGE3_PROCESSED="$STATE_DIR/.stage3_processed_ids"
STAGE3B_PROCESSED="$STATE_DIR/.stage3b_processed_ids"

# Log files
LOG_FILE="$LOG_DIR/analysis.log"

_DAEMON_MODE=0

# Batch mode time filters (set by batch command)
BATCH_START_TIME=""
BATCH_END_TIME=""

# Ensure directory structure exists
mkdir -p "$STATE_DIR" "$CACHE_DIR" "$LOG_DIR" "$SESSIONS_DIR" "$CL_DIR/prompts" "$CL_DIR/instincts/personal" "$CL_DIR/instincts/scripts"

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

# Extract JSON object from LLM raw output
# Usage: extract_json <raw_file> <output_file>
extract_json() {
    local raw_file="$1"
    local output_file="$2"
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
" < "$raw_file" > "$output_file"
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

    # Move scripts to instincts/scripts/
    local scripts_moved=0
    if [ -d "$staging_dir/scripts" ]; then
        for f in "$staging_dir/scripts"/*.sh "$staging_dir/scripts"/*.py; do
            [ -f "$f" ] || continue
            local script_basename
            script_basename=$(basename "$f")
            cp "$f" "$CL_DIR/instincts/scripts/$script_basename"
            chmod +x "$CL_DIR/instincts/scripts/$script_basename"
            scripts_moved=$((scripts_moved + 1))
        done
        log "  [$stage_name] Moved $scripts_moved scripts to instincts/scripts/"
    fi

    # Move _scripts.md to rules/
    if [ -f "$staging_dir/_scripts.md" ]; then
        cp "$staging_dir/_scripts.md" "$HOME/.claude/rules/scripts.md"
        log "  [$stage_name] Updated scripts.md"
    fi

    # Sync bash_insights.md if any bash_pattern instincts exist
    if ls "$staging_dir"/bash_pattern_*.yaml 1>/dev/null 2>&1 || \
       ls "$CL_DIR/instincts/personal"/bash_pattern_*.yaml 1>/dev/null 2>&1; then
        python3 "$CL_DIR/cli/instinct_cli.py" bash-insight sync 2>>"$LOG_FILE" || true
    fi

    # Clean up staging dir
    rm -rf "$staging_dir"

    log "  [$stage_name] Staged files applied: $moved moved, $deleted deleted, $scripts_moved scripts"
}

# ═══════════════════════════════════════════════════════════════════
# Stage Functions — each is independently callable
# ═══════════════════════════════════════════════════════════════════

# Stage 1: Session segmentation (fast, no LLM)
# Updates .last_size checkpoint on success.
stage_1_segment() {
    log "Stage 1: Segmenting sessions..."

    if [ ! -f "$JSONL" ]; then
        log "No turns.jsonl found, skipping"
        return 0
    fi

    local current_size
    current_size=$(stat -c%s "$JSONL" 2>/dev/null || stat -f%z "$JSONL" 2>/dev/null || echo 0)
    local last_size
    last_size=$(cat "$LAST_SIZE_FILE" 2>/dev/null || echo 0)

    # In batch mode, skip size guard
    if [ -z "$BATCH_START_TIME" ]; then
        if [ "$current_size" -le "$last_size" ]; then
            log "No new data (size: $current_size, last: $last_size)"
            return 0
        fi
        log "New data detected (size: $current_size, last: $last_size)"
    else
        log "Batch mode — skipping size guard"
    fi

    if python3 "$OBSERVER_DIR/segment_sessions.py" \
        --input "$JSONL" \
        --outdir "$SESSIONS_DIR" \
        --index "$INDEX" \
        --config "$CONFIG" 2>&1 | log_pipe; then
        # Update size checkpoint
        echo "$current_size" > "$LAST_SIZE_FILE"
        log "Stage 1 complete"
        return 0
    else
        log "ERROR: Stage 1 failed"
        return 1
    fi
}

# Stage 2: Two-stage task classification (LLM)
# Runs 2a (intra-session) then 2b (cross-session merge).
stage_2_classify() {
    log "Stage 2: Starting task classification..."

    # Check turn count
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
        log "Stage 2: Only $turn_count turns (need $MIN_TURNS), skipping"
        return 0
    fi

    # Build time filter args
    local time_args=""
    if [ -n "$BATCH_START_TIME" ]; then
        time_args="$time_args --start-time $BATCH_START_TIME"
    fi
    if [ -n "$BATCH_END_TIME" ]; then
        time_args="$time_args --end-time $BATCH_END_TIME"
    fi

    # ── Stage 2a: Intra-session task segmentation ──
    local stage2a_dir="$CACHE_DIR/stage2a"
    mkdir -p "$stage2a_dir"
    local stage2a_manifest="$CACHE_DIR/stage2a_manifest.json"
    local prepare_output
    prepare_output=$(python3 "$OBSERVER_DIR/prepare_stage2a.py" \
        --index "$INDEX" \
        --cursor "$CURSOR" \
        --sessions-dir "$SESSIONS_DIR" \
        --output-dir "$stage2a_dir" \
        --manifest "$stage2a_manifest" \
        $time_args 2>>"$LOG_FILE") || true

    local new_count
    new_count=$(echo "$prepare_output" | grep -oP 'new_count=\K[0-9]+' || echo 0)

    if [ "$new_count" -eq 0 ]; then
        log "Stage 2: No new sessions to classify"
        return 0
    fi

    log "Stage 2a: Segmenting $new_count sessions with $MODEL_TASK..."

    # Build Stage 2a prompt
    local stage2a_prompt_file="$CACHE_DIR/stage2a_prompt"
    {
        cat "$OBSERVER_DIR/stage2a_prompt.md"
        echo ""
        echo "Manifest file: $stage2a_manifest"
    } > "$stage2a_prompt_file"

    # Run Stage 2a LLM
    local stage2a_raw="$CACHE_DIR/stage2a_raw_output"
    cat "$stage2a_prompt_file" | CL_OBSERVER=1 CLAUDECODE= claude --model "$MODEL_TASK" --print \
        --allowedTools "Read" \
        >"$stage2a_raw" 2>"$LOG_DIR/stage2a_stderr"

    local stage2a_ok=0
    if [ -s "$stage2a_raw" ]; then
        local stage2a_ops="$CACHE_DIR/stage2a_ops.json"
        if extract_json "$stage2a_raw" "$stage2a_ops" 2>>"$LOG_DIR/stage2a_stderr"; then
            local candidates_file="$CACHE_DIR/stage2b_candidates.json"
            python3 "$OBSERVER_DIR/apply_stage2a.py" \
                --ops "$stage2a_ops" \
                --registry "$REGISTRY" \
                --manifest "$stage2a_manifest" \
                --output "$candidates_file" 2>>"$LOG_FILE"
            stage2a_ok=1
            log "Stage 2a complete"
        else
            log "ERROR: Stage 2a - failed to extract valid JSON from LLM output"
        fi
    else
        log "ERROR: Stage 2a - no output from LLM"
        cat "$LOG_DIR/stage2a_stderr" >> "$LOG_FILE" 2>/dev/null
    fi

    # ── Stage 2b: Cross-session merging ──
    if [ "$stage2a_ok" -eq 1 ]; then
        log "Stage 2b: Merging/associating candidates with $MODEL_TASK..."

        local stage2b_prompt_file="$CACHE_DIR/stage2b_prompt"
        {
            cat "$OBSERVER_DIR/stage2b_prompt.md"
            echo ""
            echo "Candidates file: $candidates_file"
        } > "$stage2b_prompt_file"

        local stage2b_raw="$CACHE_DIR/stage2b_raw_output"
        cat "$stage2b_prompt_file" | CL_OBSERVER=1 CLAUDECODE= claude --model "$MODEL_TASK" --print \
            --allowedTools "Read" \
            >"$stage2b_raw" 2>"$LOG_DIR/stage2b_stderr"

        if [ -s "$stage2b_raw" ]; then
            local stage2b_ops="$CACHE_DIR/stage2b_ops.json"
            if extract_json "$stage2b_raw" "$stage2b_ops" 2>>"$LOG_DIR/stage2b_stderr"; then
                python3 "$OBSERVER_DIR/apply_stage2b.py" \
                    --ops "$stage2b_ops" \
                    --candidates "$candidates_file" \
                    --registry "$REGISTRY" \
                    --cursor "$CURSOR" \
                    --sessions-dir "$SESSIONS_DIR" \
                    --manifest "$stage2a_manifest" 2>>"$LOG_FILE"

                log "Stage 2b complete"
                return 0
            else
                log "ERROR: Stage 2b - failed to extract valid JSON from LLM output"
            fi
        else
            log "ERROR: Stage 2b - no output from LLM"
            cat "$LOG_DIR/stage2b_stderr" >> "$LOG_FILE" 2>/dev/null
        fi
    fi

    log "ERROR: Stage 2 failed"
    return 1
}

# Stage 3: Task-centric pattern analysis (LLM)
# Reads dirty tasks from registry, independent of Stage 2 state.
stage_3_patterns() {
    log "Stage 3: Starting pattern analysis..."

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
        return 0
    elif [ "$dirty_count" -lt "$MIN_DIRTY_TASKS" ]; then
        log "Stage 3: Only $dirty_count dirty tasks (need $MIN_DIRTY_TASKS), skipping"
        return 0
    fi

    log "Stage 3: Analyzing $dirty_count dirty tasks with $MODEL_PATTERN..."
    mkdir -p "$CL_DIR/instincts/personal"

    # Enrich trajectories with transcript data (optional step)
    local enriched_bundle="$CACHE_DIR/stage3_enriched_bundle.json"
    local use_bundle="$bundle"
    if python3 "$OBSERVER_DIR/enrich_trajectories.py" \
        --bundle "$bundle" \
        --sessions-dir "$SESSIONS_DIR" \
        --output "$enriched_bundle" \
        --config "$CONFIG" 2>>"$LOG_FILE"; then
        use_bundle="$enriched_bundle"
        log "  Stage 3: Enriched trajectories with transcript data"
    else
        log "  Stage 3: Enrichment skipped (using basic bundle)"
    fi

    local staging_dir
    staging_dir=$(mktemp -d "/tmp/cl_stage3_XXXXXX")

    local stage3_prompt
    stage3_prompt="$(cat "$OBSERVER_DIR/task_analysis_prompt.md")

Task bundle file: $use_bundle
Existing instincts directory: $CL_DIR/instincts/personal/
Staging directory (write all output files here): $staging_dir/"

    echo "$stage3_prompt" | CL_OBSERVER=1 CLAUDECODE= claude --model "$MODEL_PATTERN" --print \
        --allowedTools "Read,Write,Glob" \
        >"$CACHE_DIR/stage3_raw_output" 2>"$LOG_DIR/stage3_stderr"

    if [ -s "$CACHE_DIR/stage3_raw_output" ]; then
        apply_staging_dir "$staging_dir" "Stage3"
        _mark_stage3_processed
        log "Stage 3 complete"
        return 0
    else
        log "ERROR: Stage 3 - no output from LLM"
        cat "$LOG_DIR/stage3_stderr" >> "$LOG_FILE" 2>/dev/null
        rm -rf "$staging_dir"
        return 1
    fi
}

# Stage 3b: Task-driven bash pattern analysis (LLM with Bash tool)
# Reads dirty tasks from registry, independent of Stage 2/3.
stage_3b_bash_patterns() {
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
        return 0
    elif [ "$bash_context_count" -lt "$MIN_BASH_EVENTS" ]; then
        log "Stage 3b: Only $bash_context_count bash contexts (need $MIN_BASH_EVENTS), skipping"
        return 0
    fi

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
        _mark_stage3b_processed
        log "Stage 3b complete"
        return 0
    else
        log "ERROR: Stage 3b - no output from LLM"
        cat "$LOG_DIR/stage3b_stderr" >> "$LOG_FILE" 2>/dev/null
        rm -rf "$staging_dir_3b"
        return 1
    fi
}

# ═══════════════════════════════════════════════════════════════════
# Per-stage dirty task tracking
# ═══════════════════════════════════════════════════════════════════

# Record which dirty task IDs a stage has processed
# Usage: _mark_stage_processed <processed_file>
_mark_stage_processed() {
    local processed_file="$1"
    if [ -f "$REGISTRY" ]; then
        python3 -c "
import json, sys, os
processed_path = sys.argv[1]
registry_path = sys.argv[2]
try:
    with open(registry_path) as f:
        reg = json.load(f)
    dirty = reg.get('dirty_task_ids', [])
    existing = set()
    try:
        with open(processed_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    existing.add(line)
    except FileNotFoundError:
        pass
    existing.update(dirty)
    tmp = processed_path + '.tmp'
    with open(tmp, 'w') as f:
        for tid in sorted(existing):
            f.write(tid + '\n')
    os.replace(tmp, processed_path)
except Exception as e:
    print(f'Error marking processed: {e}', file=sys.stderr)
" "$processed_file" "$REGISTRY" 2>>"$LOG_FILE" || true
    fi
}

_mark_stage3_processed()  { _mark_stage_processed "$STAGE3_PROCESSED"; }
_mark_stage3b_processed() { _mark_stage_processed "$STAGE3B_PROCESSED"; }

# Clear dirty_task_ids only for IDs processed by BOTH Stage 3 and 3b
clear_dirty_if_complete() {
    if [ ! -f "$REGISTRY" ]; then
        return 0
    fi

    python3 -c "
import json, os

registry_path = '$REGISTRY'
s3_path = '$STAGE3_PROCESSED'
s3b_path = '$STAGE3B_PROCESSED'

try:
    with open(registry_path) as f:
        reg = json.load(f)
except Exception:
    exit(0)

dirty = set(reg.get('dirty_task_ids', []))
if not dirty:
    exit(0)

# Read processed sets
def read_ids(path):
    try:
        with open(path) as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()

s3_done = read_ids(s3_path)
s3b_done = read_ids(s3b_path)

# IDs processed by both stages
both_done = s3_done & s3b_done
to_clear = dirty & both_done

if not to_clear:
    exit(0)

# Remove cleared IDs from dirty list
reg['dirty_task_ids'] = [tid for tid in reg.get('dirty_task_ids', []) if tid not in to_clear]

tmp = registry_path + '.tmp'
with open(tmp, 'w') as f:
    json.dump(reg, f, indent=2, ensure_ascii=False)
os.replace(tmp, registry_path)

# Remove cleared IDs from processed files
for path, ids in [(s3_path, s3_done), (s3b_path, s3b_done)]:
    remaining = ids - to_clear
    if remaining:
        with open(path, 'w') as f:
            for tid in sorted(remaining):
                f.write(tid + '\n')
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

print(f'Cleared {len(to_clear)} dirty task IDs')
" 2>>"$LOG_FILE" | log_pipe || true
}

# ═══════════════════════════════════════════════════════════════════
# Queue Processing
# ═══════════════════════════════════════════════════════════════════

# Process queued stages from pending_stages.json
# Uses iterative loop instead of recursion to handle cascading triggers safely.
process_queue() {
    while true; do
        # Drain queue atomically (uses file locking to avoid race with enqueue)
        local stages
        stages=$(python3 -c "
import sys
sys.path.insert(0, '$CL_DIR/hooks')
from trigger_evaluator import drain_queue
stages = drain_queue()
print(' '.join(stages))
" 2>/dev/null)

        [ -z "$stages" ] && break

        log "Processing queued stages: $stages"

        for stage in $stages; do
            case "$stage" in
                stage2)  stage_2_classify || true ;;
                stage3)  stage_3_patterns || true ;;
                stage3b) stage_3b_bash_patterns || true ;;
                *)       log "Unknown stage: $stage" ;;
            esac

            # After each stage, evaluate downstream triggers
            python3 "$CL_DIR/hooks/trigger_evaluator.py" after_stage "$stage" \
                2>>"$LOG_FILE" || true
        done

        # Clear dirty tasks processed by both Stage 3 and 3b
        clear_dirty_if_complete || true
    done
}

# Legacy run_once: force-evaluate all triggers, then process queue
run_once() {
    log "Starting analysis cycle (force_all)"

    # Run Stage 1 + queue all eligible stages
    python3 "$CL_DIR/hooks/trigger_evaluator.py" force_all \
        2>>"$LOG_FILE" || true

    # Process whatever was queued
    process_queue

    log "Analysis cycle complete"
}

# Main poll loop
run_loop() {
    _DAEMON_MODE=1
    log "Observer daemon starting (interval: ${INTERVAL}s)"

    while true; do
        sleep "$INTERVAL"
        process_queue || true
    done
}

# ═══════════════════════════════════════════════════════════════════
# Command dispatch
# ═══════════════════════════════════════════════════════════════════
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
    print(f'Dirty tasks: {len(dirty)} ({\", \".join(dirty[:5])}{\"...\" if len(dirty) > 5 else \"\"})')
" 2>/dev/null || true
        fi
        # Instincts
        instinct_count=$(ls "$CL_DIR/instincts/personal"/*.yaml 2>/dev/null | wc -l || echo 0)
        echo "Instincts: $instinct_count personal instincts"
        # Stage triggers
        echo ""
        echo "=== Stage Triggers ==="
        python3 -c "
import json

config_path = '$CONFIG'
counters_path = '$COUNTERS_FILE'
queue_path = '$QUEUE_FILE'

try:
    with open(config_path) as f:
        config = json.load(f)
except Exception:
    config = {}

pipeline = config.get('pipeline', {})

try:
    with open(counters_path) as f:
        counters = json.load(f)
except Exception:
    counters = {}

try:
    with open(queue_path) as f:
        queue = json.load(f)
except Exception:
    queue = []

for stage_name in ('stage1', 'stage2', 'stage3', 'stage3b'):
    stage_cfg = pipeline.get(stage_name, {})
    trigger = stage_cfg.get('trigger', {})
    guard = stage_cfg.get('guard', {})
    counter = counters.get(stage_name, {})

    runs = counter.get('runs_since_trigger', 0)
    total = counter.get('total_runs', 0)
    ttype = trigger.get('type', 'unknown')

    if ttype == 'on_hook':
        desc = f'trigger: on {trigger.get(\"event\", \"?\")} hook'
        print(f'{stage_name}: {total} total runs ({desc})')
    elif ttype == 'after_stage':
        count = trigger.get('count', 1)
        upstream = trigger.get('stage', '?')
        guard_desc = ''
        if guard:
            guard_parts = [f'{k}={v}' for k, v in guard.items()]
            guard_desc = f', guard: {\", \".join(guard_parts)}'
        print(f'{stage_name}: {runs}/{count} runs since last trigger (after {upstream} x{count}{guard_desc})')

if queue:
    print(f'Pending queue: {json.dumps(queue)}')
else:
    print('Pending queue: []')
" 2>/dev/null || true
        ;;

    run)
        run_once
        ;;

    stage1)
        stage_1_segment
        ;;

    stage2)
        stage_2_classify
        ;;

    stage3)
        stage_3_patterns
        ;;

    stage3b)
        stage_3b_bash_patterns
        ;;

    queue)
        cat "$QUEUE_FILE" 2>/dev/null || echo "[]"
        ;;

    clear)
        clear_dirty_if_complete
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
        echo "Usage: $0 {start|stop|status|run|stage1|stage2|stage3|stage3b|queue|clear|batch}"
        echo ""
        echo "  start   Start observer daemon in background"
        echo "  stop    Stop running observer daemon"
        echo "  status  Check observer daemon status and stage triggers"
        echo "  run     Run full pipeline (force_all + process queue)"
        echo "  stage1  Run Stage 1 only (session segmentation)"
        echo "  stage2  Run Stage 2 only (task classification)"
        echo "  stage3  Run Stage 3 only (pattern analysis)"
        echo "  stage3b Run Stage 3b only (bash pattern analysis)"
        echo "  queue   Show pending stage queue"
        echo "  clear   Clear dirty tasks processed by both stages"
        echo "  batch   Process a time window (--start-time ISO --end-time ISO)"
        exit 1
        ;;
esac
