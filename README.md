# Continual Learning for Claude Code

A self-improving observation system that watches how you use Claude Code, discovers recurring behavioral patterns across sessions, and materializes them into persistent rules that improve future interactions. No manual labeling required — the system learns from natural usage.

## Table of Contents

- [Installation & Configuration](#installation--configuration)
- [Quick Start](#quick-start)
- [Architecture Overview](#architecture-overview)
- [Data Pipeline](#data-pipeline)
  - [Layer 0: Hook Recording](#layer-0-hook-recording)
  - [Layer 1: Session Segmentation (Stage 1)](#layer-1-session-segmentation-stage-1)
  - [Layer 2: Task Discovery (Stage 2a + 2b)](#layer-2-task-discovery-stage-2a--2b)
  - [Layer 3: Pattern Mining (Stage 3 + 3b)](#layer-3-pattern-mining-stage-3--3b)
  - [Layer 4: Materialization & Interception](#layer-4-materialization--interception)
- [Event-Driven Pipeline Triggers](#event-driven-pipeline-triggers)
- [Academic Foundations](#academic-foundations)
- [CLI Reference](#cli-reference)

---

## Installation & Configuration

### Prerequisites

- Claude Code CLI installed and working
- Python 3.10+

### Plugin Registration

The system is registered as a Claude Code plugin. Enable it in `~/.claude/settings.json`:

```json
{
  "enabledPlugins": {
    "continual-learning@continual-learning": true
  }
}
```

The plugin descriptor lives at `.claude-plugin/plugin.json`:

```json
{
  "name": "continual-learning",
  "version": "4.0.0",
  "description": "Automatic behavior observation and pattern learning for Claude Code",
  "skills": ["./skills/"],
  "agents": []
}
```

Once enabled, all hooks are automatically registered via `hooks/hooks.json` — covering 7 Claude Code hook events (PreToolUse, PostToolUse, UserPromptSubmit, PostToolUseFailure, Stop, SubagentStart, SubagentStop). No manual hook configuration is needed.

### Configuration: `config.json`

All system behavior is controlled by a single configuration file at the project root.

```json
{
  "version": "4.0",

  "data": {
    "max_file_size_mb": 50          // turns.jsonl rotation threshold
  },

  "tool_recording": {
    "default": {
      "input": "target_only",       // record only the primary target field
      "target_fields": ["file_path", "pattern", "url"],
      "output": "none"              // don't record tool output by default
    },
    "overrides": {
      "Bash":      { "input": "detailed", "fields": ["command"], "output": "full" },
      "Grep":      { "input": "detailed", "fields": ["pattern", "path", "glob", "type"] },
      "WebSearch": { "input": "detailed", "fields": ["query"] },
      "WebFetch":  { "input": "detailed", "fields": ["url", "prompt"] },
      "Task":      { "input": "delegate", "fields": ["subagent_type", "prompt"] },
      "Skill":     { "input": "skill",    "fields": ["skill", "args"] }
    },
    "ignore": ["TodoWrite", "TodoRead"]   // never record these tools
  },

  "observer": {
    "interval_seconds": 600,        // daemon poll interval
    "model_episode": "sonnet",      // LLM for task classification (Stage 2)
    "model_pattern": "sonnet",      // LLM for pattern detection (Stage 3)
    "model_bash_pattern": "sonnet", // LLM for bash pattern detection (Stage 3b)
    "min_turns_to_analyze": 14,     // minimum turns before running Stage 2
    "min_episodes_for_patterns": 3, // minimum task count for pattern detection
    "min_bash_events": 10,          // minimum bash contexts for Stage 3b
    "min_dirty_tasks_for_stage3": 1,// minimum dirty tasks for Stage 3
    "time_gap_minutes": 30          // session boundary threshold
  },

  "enrichment": {
    "max_transcript_size_mb": 2,    // skip enrichment for huge transcripts
    "max_enriched_turns_per_task": 30,
    "max_action_chain_blocks": 50,  // max action blocks per turn
    "max_subagent_summaries": 10    // max subagent summaries per task
  },

  "instincts": {
    "min_confidence": 0.3,          // new instinct minimum confidence
    "auto_apply_threshold": 0.7,    // confidence for materialization
    "min_observations": 3,          // minimum supporting evidence
    "confidence_decay_per_week": 0.02,
    "max_instincts": 100
  },

  "bash_intercept": {
    "enabled": true,
    "block_threshold": 0.7,         // confidence >= this → block command
    "warn_threshold": 0.5,          // confidence >= this → warn only
    "cooldown_seconds": 5           // minimum interval between blocks
  },

  "materialization": {
    "rule_threshold": 0.7           // materialize instinct to rule file
  },

  "pipeline": {
    "stage1":  { "trigger": { "type": "on_hook", "event": "Stop" } },
    "stage2":  { "trigger": { "type": "after_stage", "stage": "stage1", "count": 3 },
                 "guard":   { "min_turns": 14 } },
    "stage3":  { "trigger": { "type": "after_stage", "stage": "stage2", "count": 1 },
                 "guard":   { "min_dirty_tasks": 1 } },
    "stage3b": { "trigger": { "type": "after_stage", "stage": "stage2", "count": 1 },
                 "guard":   { "min_bash_events": 10 } }
  }
}
```

#### Key Configuration Sections

| Section | Purpose | Tuning Guidance |
|---------|---------|-----------------|
| `tool_recording` | Controls what data is captured per tool. `"detailed"` records all specified fields; `"target_only"` records just the primary file/pattern/URL; `"none"` skips output. | Add `"output": "full"` to any tool override to capture its output for richer analysis. |
| `observer` | Controls when and how the LLM analysis stages run. | Increase `interval_seconds` to reduce daemon overhead. Lower `min_turns_to_analyze` for faster first analysis. |
| `enrichment` | Budget controls for transcript enrichment (Stage 3 preprocessing). | Lower limits if LLM context is a bottleneck; raise for richer pattern detection. |
| `instincts` | Confidence lifecycle parameters. | Raise `min_observations` if instincts are too noisy; lower `confidence_decay_per_week` if patterns age out too fast. |
| `bash_intercept` | Real-time Bash command blocking based on learned instincts. | Set `enabled: false` to disable all interception. |
| `pipeline` | Per-stage trigger rules (see [Pipeline Triggers](#event-driven-pipeline-triggers)). | Change `stage2.trigger.count` to control how many sessions accumulate before LLM analysis. |

### Hook Routing: `hooks/routing.json`

Controls which sub-hooks run for each event type:

```json
{
  "pre_tool":    { "*": ["record"], "Bash": ["record", "intercept"] },
  "post_tool":   { "Bash": ["record"] },
  "user_prompt": { "*": ["record"] },
  "tool_fail":   { "*": ["record"] },
  "stop":        { "*": ["record", "trigger"] },
  "subagent_start": { "*": ["record"] },
  "subagent_stop":  { "*": ["record"] }
}
```

Three sub-hooks exist:
- **`record`** — Always runs. Appends a structured JSONL event to `data/turns.jsonl`. Never blocks.
- **`intercept`** — Bash-only. Matches commands against learned `bash_pattern` instincts. May block (exit 2) or warn.
- **`trigger`** — Stop-only. Evaluates pipeline trigger rules, runs Stage 1, may queue downstream stages.

---

## Quick Start

```bash
# View system status (sessions, tasks, instincts, pipeline triggers)
~/.claude/continual-learning/observer/daemon.sh status

# Run the full analysis pipeline once (for testing)
~/.claude/continual-learning/observer/daemon.sh run

# Start the background daemon (polls every 10 minutes)
~/.claude/continual-learning/observer/daemon.sh start

# View instinct summary
python3 ~/.claude/continual-learning/cli/instinct_cli.py status

# Use the /cl skill in Claude Code for a quick overview
# (type /cl in a Claude Code session)
```

In normal operation, you don't need to do anything. The system:
1. Records events automatically via hooks (zero-overhead, no LLM)
2. Segments sessions on every `Stop` event (fast Python, no LLM)
3. Queues LLM analysis after every 3 sessions (configurable)
4. Produces instinct files that feed back into real-time interception and rule materialization

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Claude Code Session                          │
│  User prompts → Tool calls → Subagent delegations → Stop           │
└───────┬──────────────┬──────────────────────┬──────────────────┬────┘
        │ PreToolUse   │ PostToolUse          │ Stop             │ ...
        ▼              ▼                      ▼                  ▼
┌───────────────────────────────────────────────────────────────────┐
│  dispatcher.sh → routing.json → [record] [intercept] [trigger]   │
└───────┬──────────────┬──────────────────────┬────────────────────┘
        │              │                      │
        ▼              ▼                      ▼
   turns.jsonl    Block/Warn          Stage 1 (sync)
   (append-only)  via instincts       + queue Stage 2
                                           │
                            ┌──────────────┘
                            ▼
                    ┌── daemon.sh ──┐
                    │ process_queue  │ (polls pending_stages.json)
                    └───┬───────────┘
                        │
              ┌─────────┼─────────┐
              ▼         ▼         ▼
          Stage 2   Stage 3   Stage 3b
          (LLM)     (LLM)    (LLM)
              │         │         │
              ▼         ▼         ▼
        task_registry  instincts/*.yaml
                       learned.md
                       scripts/
```

---

## Data Pipeline

### Directory Layout

```
~/.claude/continual-learning/
├── config.json                    # All system configuration
├── hooks/
│   ├── hooks.json                 # Claude Code hook registration
│   ├── routing.json               # Sub-hook routing rules
│   ├── dispatcher.sh              # Unified hook entry point
│   ├── record.py                  # Event recording sub-hook
│   ├── intercept.py               # Bash interception sub-hook
│   └── trigger_evaluator.py       # Pipeline trigger logic
├── observer/
│   ├── daemon.sh                  # Daemon + stage functions + CLI
│   ├── segment_sessions.py        # Stage 1: session segmentation
│   ├── prepare_stage2a.py         # Stage 2a pre-processor
│   ├── apply_stage2a.py           # Stage 2a post-processor
│   ├── apply_stage2b.py           # Stage 2b post-processor
│   ├── prepare_stage3.py          # Stage 3 pre-processor
│   ├── enrich_trajectories.py     # Stage 3 transcript enricher
│   ├── extract_bash_contexts.py   # Stage 3b pre-processor
│   ├── transcript_reader.py       # Claude Code JSONL transcript parser
│   ├── stage2a_prompt.md          # Stage 2a LLM prompt
│   ├── stage2b_prompt.md          # Stage 2b LLM prompt
│   ├── task_analysis_prompt.md    # Stage 3 LLM prompt
│   └── bash_pattern_prompt.md     # Stage 3b LLM prompt
├── data/
│   ├── turns.jsonl                # Raw event stream (append-only)
│   ├── turns.archive/             # Rotated JSONL archives
│   ├── sessions/                  # Per-session JSON + _index.json
│   ├── task_registry.json         # Core persistent state
│   ├── state/                     # Pipeline state files
│   ├── cache/                     # Intermediate artifacts (rebuildable)
│   └── log/                       # Daemon and stage logs
├── instincts/
│   ├── personal/                  # Learned instinct YAML files (final output)
│   └── scripts/                   # Automatable scripts generated by LLM
├── prompts/                       # Optimized subagent prompts
└── cli/
    └── instinct_cli.py            # CLI for instinct management
```

---

### Layer 0: Hook Recording

Every Claude Code interaction generates hook events that flow through `dispatcher.sh` and get recorded to `turns.jsonl`.

#### `data/turns.jsonl` — Raw Event Stream

Each line is a JSON object with a common header and event-specific fields:

```
Common: { "v": 4, "ts": "ISO-8601", "sid": "session-uuid", "e": "<event_type>" }
```

**Event types:**

| `e` | Hook Source | Key Fields | Description |
|-----|-----------|------------|-------------|
| `turn` | UserPromptSubmit | `prompt`, `cwd`, `tp` | User enters a prompt |
| `tool` | PreToolUse (target_only) | `tool`, `target` | Tool call — only primary target recorded |
| `tool` | PreToolUse (detailed) | `tool`, `input{...}` | Tool call — full input fields recorded |
| `delegate` | PreToolUse (Task) | `agent`, `agent_prompt` | Subagent delegation |
| `skill` | PreToolUse (Skill) | `skill`, `args` | Skill invocation (e.g., /commit) |
| `bash_ok` | PostToolUse (Bash) | `cmd`, `out` | Bash command with full stdout |
| `fail` | PostToolUseFailure | `tool`, `error` | Tool execution failure |
| `agent_start` | SubagentStart | `agent`, `agent_id` | Subagent launched |
| `agent_stop` | SubagentStop | `agent`, `agent_id`, `response`, `atp` | Subagent completed |
| `stop` | Stop | `response`, `tp` | Session ended |

**Design insight — Selective fidelity:** Not all tools need the same recording depth. The `tool_recording` config implements a tiered scheme: Bash gets full input+output (critical for pattern detection), most tools get only their primary target (file path or URL), and some tools are ignored entirely. This keeps `turns.jsonl` compact while preserving the signals that matter for pattern mining. See [Selective Attention](#selective-attention) in Academic Foundations.

---

### Layer 1: Session Segmentation (Stage 1)

**Script:** `observer/segment_sessions.py`
**Trigger:** Every `Stop` hook (synchronous, no LLM)
**Cost:** Zero LLM cost — pure Python processing

Groups raw JSONL events by `sid` (session ID) into per-session JSON files, extracting structural signals for downstream task discovery.

#### `data/sessions/{sid}.json` — Per-Session File

```json
{
  "session_id": "eda83a09-...",
  "time_range": { "start": "ISO", "end": "ISO" },
  "primary_cwd": "/root/project",
  "all_cwds": ["/root/project", "/root/other"],
  "turn_count": 9,
  "event_count": 93,
  "has_stop": true,
  "transcript_path": "/root/.claude/projects/.../uuid.jsonl",
  "signals": {
    "cwd_switches":     [{ "at_turn": 3, "from": "/a", "to": "/b" }],
    "time_gaps":        [{ "after_turn": 2, "gap_minutes": 45.3 }],
    "prompt_keywords":  ["observer", "pipeline", "refactor"],
    "session_adjacency": {
      "prev_sid": "uuid", "prev_gap_minutes": 12.5,
      "next_sid": "uuid", "next_gap_minutes": 5.0
    }
  },
  "turns": [{
    "turn_idx": 0,
    "ts": "ISO",
    "prompt": "Full user prompt text",
    "cwd": "/working/dir",
    "tools":         { "Bash": 3, "Read": 5, "Glob": 2 },
    "files_touched": ["/path/to/file.py"],
    "delegates":     [{ "agent": "Explore", "prompt_preview": "first 200 chars..." }],
    "bash_commands":  ["npm test", "git status"],
    "fail_count": 1,
    "duration_ms": 45000
  }]
}
```

**Design insight — Temporal segmentation signals:** The `signals` object extracts features that help the LLM (Stage 2) detect task boundaries without reading raw JSONL: working directory switches often indicate project changes; large time gaps suggest context switches; prompt keywords provide semantic anchors; session adjacency reveals multi-session task continuations. This is a form of [Feature Engineering for Episode Boundaries](#episode-boundary-detection).

#### `data/sessions/_index.json` — Session Index

```json
{
  "built_at": "ISO",
  "total_sessions": 9,
  "total_turns": 20,
  "sessions": [{
    "sid": "uuid", "start": "ISO", "end": "ISO",
    "primary_cwd": "/root/...", "turn_count": 9,
    "event_count": 93, "has_stop": true
  }]
}
```

Consumed by `prepare_stage2a.py` (to find new sessions) and `trigger_evaluator.py` (for `min_turns` guard).

---

### Layer 2: Task Discovery (Stage 2a + 2b)

Two-phase LLM pipeline that transforms sessions into a persistent task registry. Stage 2a segments within sessions; Stage 2b merges across sessions.

**Design insight — Two-phase task discovery:** A single LLM call cannot efficiently handle both intra-session segmentation and cross-session merging. Stage 2a focuses narrowly on one session at a time (tractable context), while 2b operates over the full candidate + existing task space. This decomposition follows the [Hierarchical Episode Discovery](#hierarchical-episode-discovery) pattern.

#### Stage 2a: Intra-Session Segmentation

**Pre-processor:** `prepare_stage2a.py`
**Incremental processing:** Only sessions not yet in `.stage2_cursor.json` are processed.

`data/cache/stage2a_manifest.json`:
```json
{
  "new_sessions": [{
    "sid": "uuid",
    "path": "/root/.../cache/stage2a/uuid.json",
    "primary_cwd": "/root/...",
    "start": "ISO",
    "turn_count": 9
  }]
}
```

`data/cache/stage2a/{sid}.json` — lightweight per-session summary (prompts, tool counts, bash commands, delegates — no raw event data).

**LLM output** (`data/cache/stage2a_ops.json`):
```json
{
  "session_segments": [{
    "sid": "session-uuid",
    "segments": [{
      "turn_range": [0, 5],
      "name": "Implement v4 data pipeline",
      "task_type": "feature",
      "description": "2-5 sentence description of what happened."
    }]
  }]
}
```

`task_type` is one of: `feature`, `bug_fix`, `research`, `config`, `explore`, `refactor`, `review`, `setup`.

**Post-processor** (`apply_stage2a.py`) produces `data/cache/stage2b_candidates.json`:
```json
{
  "new_candidates": [{
    "candidate_id": "cand-001",
    "name": "...", "task_type": "feature", "description": "...",
    "fragments": [{ "sid": "uuid", "turn_range": [0, 5] }],
    "primary_cwd": "/root/...",
    "time_range": { "start": "ISO", "end": "ISO" }
  }],
  "existing_tasks": [{
    "task_id": "task-001", "name": "...", "description": "...",
    "status": "active", "primary_cwd": "/root/...", "fragment_count": 3
  }]
}
```

#### Stage 2b: Cross-Session Merging

**LLM output** (`data/cache/stage2b_ops.json`) — 6 operation types:

| Operation | Description |
|-----------|-------------|
| `create_task` | New task from a candidate |
| `append_to_existing` | Attach candidate as a continuation/revisit fragment to an existing task |
| `merge_candidates` | Merge multiple candidates into a single new task |
| `mark_non_task` | Discard a candidate (one-shot test, greeting, etc.) |
| `update_status` | Mark a task as completed |
| `add_relation` | Create inter-task relations (`spawned_by`, `blocks`, `related`) |

**Post-processor** (`apply_stage2b.py`) writes to the core state:

#### `data/task_registry.json` — Core Persistent State

```json
{
  "version": 1,
  "updated_at": "ISO",
  "dirty_task_ids": ["task-003", "task-004"],
  "next_task_num": 5,
  "tasks": {
    "task-001": {
      "task_id": "task-001",
      "name": "Implement v4 data pipeline",
      "description": "Multi-sentence description...",
      "task_type": "feature",
      "status": "active",
      "primary_cwd": "/root/.claude/continual-learning",
      "created_at": "ISO",
      "updated_at": "ISO",
      "fragments": [
        { "sid": "session-uuid", "turn_range": [0, 5], "role": "origin" },
        { "sid": "other-uuid",   "turn_range": [3, 7], "role": "continuation" }
      ],
      "relations": [
        { "task_id": "task-002", "relation": "spawned_by" }
      ]
    }
  },
  "non_tasks": [
    { "sid": "unknown", "reason": "single-turn session with no meaningful work" }
  ]
}
```

`dirty_task_ids` is the central signal — newly created or updated tasks are marked dirty, which triggers Stage 3/3b analysis. Dirty IDs are only cleared after **both** Stage 3 and Stage 3b have processed them.

**Design insight — Dirty flag propagation:** The `dirty_task_ids` mechanism implements a [Change Data Capture](#change-data-capture) pattern. Rather than re-analyzing all tasks every cycle, only modified tasks flow into the expensive LLM stages. This makes the pipeline cost-proportional to change volume, not total data volume.

#### `data/state/.stage2_cursor.json` — Incremental Processing Cursor

```json
{
  "processed_sessions": {
    "session-uuid": { "event_count": 93, "processed_at": "ISO" }
  }
}
```

Enables incremental Stage 2a processing: sessions whose `event_count` in `_index.json` exceeds the cursor value are reprocessed (handles sessions that were open during the last analysis).

---

### Layer 3: Pattern Mining (Stage 3 + 3b)

Two parallel LLM analysis paths that consume dirty tasks and produce instinct YAML files.

#### Stage 3: Task-Centric Pattern Analysis

**Pre-processor** (`prepare_stage3.py`) builds `data/cache/stage3_bundle.json`:

```json
{
  "dirty_tasks": [{
    "task_id": "task-003",
    "name": "...", "task_type": "refactor", "status": "active",
    "trajectory": [
      { "sid": "uuid", "turn_idx": 0, "ts": "ISO", "prompt": "...",
        "cwd": "/root/...", "tools": {"Bash": 3},
        "files_touched": ["/path/file.py"], "delegates": [],
        "bash_commands": ["npm test"], "fail_count": 0, "duration_ms": 45000 },
      { "_session_break": true, "gap_minutes": 12.5 },
      { "sid": "other-uuid", "turn_idx": 0, "..." : "..." }
    ]
  }]
}
```

The trajectory stitches all fragments chronologically with `_session_break` markers between sessions, giving the LLM a complete view of how a task evolved across multiple sessions.

**Enrichment** (`enrich_trajectories.py`) optionally augments the bundle by reading Claude Code's raw transcript JSONL files. Enriched turns gain an `action_chain` field showing the LLM's internal reasoning:

```json
"action_chain": [
  { "type": "thinking", "text": "truncated..." },
  { "type": "tool_use", "tool": "Bash", "input_summary": "npm test" },
  { "type": "tool_result", "tool": "Bash", "output_summary": "3 tests passed" }
]
```

Tasks may also gain `subagent_summaries` showing delegated agent work.

**Design insight — Trajectory enrichment:** The basic bundle provides what the user did; the enriched bundle adds *why the agent did it* (thinking blocks) and *what happened* (tool results). This dual view enables the LLM to distinguish between user-driven corrections (the user changed approach) and agent-driven iterations (the agent tried something, failed, retried). See [Hindsight Experience Replay](#hindsight-experience-replay).

The Stage 3 LLM detects **8 pattern types:**

| Pattern Type | Description | Example |
|---|---|---|
| `strategy_selection` | Which strategies work best for which task types | "Launch Explore agent before coding in unfamiliar codebases" |
| `correction_pattern` | User repeatedly corrects the same behavior (≥2 occurrences) | "User corrects agent from using `find` to using `Glob`" |
| `delegation_preference` | Effective vs ineffective subagent prompt styles | "Structured Explore prompts with numbered questions work better" |
| `efficiency_hint` | Fast vs slow approaches to similar tasks | "Plan mode before multi-file changes reduces backtracking" |
| `file_cochange` | Files that consistently change together | "schema.ts and migration.sql always co-change" |
| `action_chain_pattern` | Recurring tool-use sequences (≥3 occurrences) | "Read → Grep → Edit cycle for bug fixes" |
| `exploration_knowledge` | Factual knowledge extracted from research tasks | "The API uses JWT, not session cookies" |
| `environment_script` | Automatable bash command sequences | "Project setup always runs these 5 commands" |

#### Stage 3b: Bash Pattern Analysis

Parallel to Stage 3 but specialized for Bash command patterns.

**Pre-processor** (`extract_bash_contexts.py`) builds `data/cache/stage3b_task_contexts.json`:

```json
{
  "tasks": [{
    "task_id": "task-001",
    "name": "...", "task_type": "feature",
    "bash_contexts": [{
      "sid": "session-uuid", "turn_idx": 2,
      "trajectory_before": [{ "turn_idx": 1, "prompt_preview": "...", "tools": {"Read": 1} }],
      "bash_call": { "command": "find . -name '*.py'", "ts": "ISO" },
      "feedback": { "type": "fail", "output_preview": "No such file..." },
      "trajectory_after": [{ "turn_idx": 3, "prompt_preview": "...", "tools": {"Glob": 1} }],
      "has_failure": true,
      "correction_candidate": true
    }]
  }]
}
```

Each bash command is contextualized with its surrounding trajectory (what happened before and after), failure status, and a `correction_candidate` flag indicating the agent likely switched to a different approach.

**Design insight — Correction detection:** The `correction_candidate` heuristic identifies self-correction episodes: when a command fails and the next turn uses different tools, or when the command succeeds but is immediately followed by a semantically different approach. This operationalizes the [Learning from Corrections](#learning-from-corrections) paradigm without requiring explicit user feedback labels.

Stage 3b produces `bash_pattern` instincts with `intercept.regex` fields that enable real-time command interception.

#### Staging and Application

Both Stage 3 and 3b write to temporary staging directories. The daemon's `apply_staging_dir()` function moves artifacts to their final locations:

| Staging File | Destination | Purpose |
|---|---|---|
| `*.yaml` | `instincts/personal/` | Instinct rules |
| `_delete_<id>` | (deletes matching instinct) | Instinct removal |
| `_learned.md` | `~/.claude/rules/learned.md` | Materialized rules injected into Claude Code context |
| `_prompt_<agent>.md` | `prompts/<agent>.md` | Optimized subagent prompts |
| `scripts/*.sh`, `*.py` | `instincts/scripts/` | Automatable environment scripts |
| `_scripts.md` | `~/.claude/rules/scripts.md` | Script documentation |

---

### Layer 4: Materialization & Interception

#### Instinct YAML Files

The final output of the pipeline. Each instinct has YAML frontmatter + Markdown body:

```yaml
---
id: strategy_selection_explore_first
type: strategy_selection
trigger: "when starting a feature task in an unfamiliar codebase"
confidence: 1.0
domain: delegation_strategy
observations: 40
first_seen: "2026-02-26"
last_seen: "2026-03-04"
source: observer_v4
intercept:                    # only for bash_pattern type
  regex: "\\bfind\\s+..."
  bypass_env: "CL_SKIP_FIND_CHECK"
---

## Pattern
Description of the observed behavioral pattern with nuances and exceptions.

## Action
Specific recommended actions.

## Evidence
- task-001 turn 3: specific supporting evidence with metrics
```

**Confidence lifecycle:**
- New instinct starts at ≥ 0.3 confidence with ≥ 3 observations
- Same-direction reinforcement: +0.05 per batch
- Contradicting evidence: -0.10 per batch
- Weekly decay: -0.02 (prevents stale instincts from persisting)
- Below 0.1: marked for deletion
- Cap at 1.0

**Design insight — Confidence as Bayesian belief:** The confidence score approximates a Bayesian posterior updated by evidence. Reinforcement acts as likelihood in favor; contradiction acts against; time decay serves as a prior toward uncertainty. This prevents both premature lock-in (high confidence from few observations) and stale persistence (old patterns that no longer apply). See [Confidence as Bayesian Belief](#confidence-lifecycle-and-bayesian-updating).

#### Real-Time Bash Interception

`intercept.py` loads all `bash_pattern` instincts and matches incoming Bash commands against their `intercept.regex`:

- **confidence ≥ 0.7 (block_threshold):** Command is blocked (exit 2). Claude Code sees the block message and adapts.
- **confidence ≥ 0.5 (warn_threshold):** Warning printed to stderr, command proceeds.
- **Cooldown:** 5 seconds between blocks to avoid rapid-fire interruptions.

Bypass: prefix command with `CL_BASH_INTERCEPT=0` or `CL_SKIP=<instinct_id>`.

#### Materialized Rules

High-confidence instincts are materialized to `~/.claude/rules/learned.md`, which Claude Code loads into its system prompt. This means learned preferences affect behavior *without* requiring runtime interception — they become part of the agent's instructions.

---

## Event-Driven Pipeline Triggers

The pipeline uses a configurable trigger system instead of running all stages on every poll cycle.

```
Stop hook → trigger_evaluator.py
                 ├─ Run Stage 1 (synchronous, fast, no LLM)
                 ├─ Increment stage1 counter
                 └─ If stage1.runs ≥ 3 AND min_turns guard passes → queue "stage2"

Daemon poll → process_queue()
                 ├─ Dequeue "stage2" → run Stage 2
                 ├─ If stage2 complete AND dirty tasks exist → queue "stage3", "stage3b"
                 ├─ Dequeue "stage3" → run Stage 3
                 ├─ Dequeue "stage3b" → run Stage 3b
                 └─ clear_dirty_if_complete()
```

**Trigger types:**
- `on_hook` — Runs synchronously when the hook event fires (Stage 1 only, no LLM)
- `after_stage` — Queued for daemon when upstream stage has run `count` times AND guard conditions pass

**Guard conditions:**
- `min_turns`: Total turns in session index must meet threshold
- `min_dirty_tasks`: Number of dirty tasks must meet threshold
- `min_bash_events`: Number of bash contexts for dirty tasks must meet threshold

**State files:**

`data/state/stage_counters.json`:
```json
{
  "stage1": { "runs_since_trigger": 2, "total_runs": 15, "last_run": "ISO" },
  "stage2": { "runs_since_trigger": 0, "total_runs": 5,  "last_run": "ISO" }
}
```

`data/state/pending_stages.json`:
```json
["stage2"]
```

The queue uses `fcntl` file locking for safe concurrent access between hooks and daemon.

---

## Academic Foundations

The system's design draws on several research areas. This section maps each design decision to its academic motivation.

### Continual Learning / Lifelong Learning

The core premise: an AI system should improve from its deployment experience without catastrophic forgetting of prior knowledge.

Traditional continual learning focuses on model weights (EWC, progressive nets). This system operates at the **meta-level** — it doesn't fine-tune the LLM, but instead modifies the *context* (rules, prompts, interception logic) that shapes the LLM's behavior. This is closer to **meta-learning** or **learning to prompt**: the system learns which instructions produce better outcomes.

The instinct confidence lifecycle (reinforcement, contradiction, decay) mirrors **Elastic Weight Consolidation (EWC)** at the symbolic level — high-confidence instincts are harder to override (analogous to EWC's Fisher Information penalty), while low-confidence instincts are easily displaced by new evidence.

> Kirkpatrick et al., "Overcoming catastrophic forgetting in neural networks," PNAS 2017.

### Episode Boundary Detection

Stage 1's session segmentation and Stage 2a's intra-session task segmentation address the **activity segmentation** problem: given a continuous stream of tool interactions, identify where one logical task ends and another begins.

The signals extracted by Stage 1 — working directory switches, time gaps, prompt keyword changes, session adjacency — are **temporal boundary features** analogous to those used in video temporal segmentation and dialog act segmentation. The 30-minute `time_gap_minutes` threshold operationalizes the **session timeout heuristic** common in web analytics.

Stage 2a's LLM-based segmentation goes beyond heuristics by using semantic understanding of prompt content and tool usage patterns to detect task switches within a single session — e.g., a user debugging a bug and then switching to write documentation.

> Zacks & Swallow, "Event Segmentation," Current Directions in Psychological Science, 2007.

### Hierarchical Episode Discovery

Stage 2's two-phase design (intra-session → cross-session) implements **hierarchical temporal abstraction**: first discover fine-grained episodes (segments within sessions), then compose them into coarse-grained episodes (tasks spanning multiple sessions).

This mirrors the **options framework** in hierarchical reinforcement learning, where primitive actions compose into temporally extended options. Here, turn-level actions compose into segments, which compose into tasks.

The `fragment` concept with `role` annotations (`origin`, `continuation`, `revisit`) explicitly models how human work is temporally distributed — a task may be started, interrupted, resumed days later, and revisited for maintenance.

> Sutton, Precup & Singh, "Between MDPs and semi-MDPs: A framework for temporal abstraction in reinforcement learning," Artificial Intelligence, 1999.

### Selective Attention

The `tool_recording` configuration implements a **selective attention** mechanism: not all observations deserve equal recording fidelity. Bash commands get full input+output capture (high information density for pattern detection), while Read tool calls get only the file path (the content is already on disk).

This is analogous to **attention mechanisms** in transformers — the system allocates recording bandwidth proportional to the expected utility of each observation type for downstream pattern mining.

The enrichment budget controls (`max_enriched_turns_per_task`, `max_action_chain_blocks`) implement a similar attention bottleneck at the analysis stage, preventing the LLM from being overwhelmed by low-signal turns.

### Change Data Capture

The `dirty_task_ids` mechanism implements **Change Data Capture (CDC)**: only modified records flow into expensive downstream processing. This is the same principle used in database replication (PostgreSQL logical decoding, Debezium) and data warehouse ETL (incremental materialized views).

Combined with the Stage 2 cursor (`.stage2_cursor.json`) which tracks processed sessions by `event_count`, the pipeline achieves **full incrementality**: no data is re-processed unless it has genuinely changed.

### Hindsight Experience Replay

Stage 3's trajectory enrichment adds the agent's internal reasoning (thinking blocks, tool results) to the behavioral trace. This enables **hindsight analysis** — the pattern-detecting LLM can understand not just what happened, but *why* the agent chose that action and *what it saw* as a result.

This is conceptually related to **Hindsight Experience Replay (HER)**: reinterpreting past trajectories with additional information that wasn't available at decision time. The enriched bundle lets the pattern detector identify moments where the agent's reasoning was correct but the action failed (bad luck) vs. moments where the reasoning itself was flawed (learnable pattern).

> Andrychowicz et al., "Hindsight Experience Replay," NeurIPS 2017.

### Learning from Corrections

Stage 3b's `correction_candidate` detection implements **learning from implicit feedback**: when a Bash command fails and the agent switches to a different tool in the next turn, that sequence is a natural correction signal — no explicit user feedback required.

This extends the **learning from demonstration** paradigm to **learning from self-correction**: the agent's own behavioral adaptation (failing with `find`, succeeding with `Glob`) becomes training signal for future behavior modification (blocking `find` via interception).

The evidence accumulated in instinct YAML files (e.g., "532 observations of find/ls anti-pattern across all tasks") functions as a **replay buffer** of correction episodes, providing statistical confidence that a pattern is real and not coincidental.

> Ross et al., "A Reduction of Imitation Learning and Structured Prediction to No-Regret Online Learning" (DAgger), AISTATS 2011.

### Confidence Lifecycle and Bayesian Updating

The instinct confidence score follows a **Bayesian belief update** model:

- **Prior:** New instincts start at 0.3 (mild belief)
- **Positive evidence:** +0.05 per reinforcing batch (likelihood ratio > 1)
- **Negative evidence:** -0.10 per contradicting batch (likelihood ratio < 1, weighted 2x to encourage conservatism)
- **Time decay:** -0.02 per week (prior drift toward uncertainty — environments change)
- **Deletion threshold:** 0.1 (belief too weak to maintain)
- **Saturation:** 1.0 cap (prevents over-confidence)

The asymmetric reinforcement/contradiction weights (+0.05/-0.10) implement a **conservative update** policy: it's cheaper to miss a real pattern than to act on a false one. This mirrors **pessimistic Q-learning** in offline RL.

The weekly decay prevents **distribution shift blindness**: patterns learned from early usage may not apply as the user's workflow evolves. Decay forces instincts to be continually re-confirmed by fresh evidence.

> Gelman et al., "Bayesian Data Analysis," 3rd edition, 2013.

### Reflexive Self-Modification

The full pipeline forms a **reflexive loop**: the system observes itself → discovers patterns → modifies its own instructions → changes its future behavior → observes the changed behavior → discovers new patterns.

This is a practical implementation of **Schmidhuber's Gödel Machine** concept: a self-referential system that modifies its own policy when it can prove the modification is beneficial. Here, "proof" is replaced by statistical confidence (observation count + confidence score), and "policy modification" is materialized rules + interception regexes.

The staging directory pattern (LLM writes to temp, daemon applies atomically) provides a **safety boundary** that prevents the self-modification from being destructive — the system can propose changes but cannot corrupt its own persistent state in a single step.

> Schmidhuber, "Gödel Machines: Fully Self-Referential Optimal Universal Self-Improvers," 2003.

---

## CLI Reference

### Daemon Commands

```bash
daemon.sh start          # Start background daemon
daemon.sh stop           # Stop background daemon
daemon.sh status         # Show status + stage triggers + queue
daemon.sh run            # Run full pipeline once (force_all + process queue)
daemon.sh stage1         # Run Stage 1 only (session segmentation)
daemon.sh stage2         # Run Stage 2 only (task classification)
daemon.sh stage3         # Run Stage 3 only (pattern analysis)
daemon.sh stage3b        # Run Stage 3b only (bash pattern analysis)
daemon.sh queue          # Show pending stage queue
daemon.sh clear          # Clear dirty tasks processed by both Stage 3 and 3b
daemon.sh batch --start-time ISO --end-time ISO   # Process a time window
```

### Instinct CLI

```bash
instinct_cli.py status                    # Show all instincts
instinct_cli.py observer start|stop|status  # Manage daemon
instinct_cli.py bash-insight list         # List bash interception rules
instinct_cli.py bash-insight sync         # Sync instincts to rules/bash_insights.md
instinct_cli.py bash-insight test "cmd"   # Test if a command would be blocked
instinct_cli.py materialize               # Generate rules from high-confidence instincts
instinct_cli.py export -o backup.yaml     # Export instincts
instinct_cli.py import teammate.yaml      # Import instincts
```

### Bypass Controls

```bash
CL_BASH_INTERCEPT=0 <command>    # Skip all bash interception for this command
CL_SKIP=<instinct_id> <command>  # Skip specific instinct for this command
```
