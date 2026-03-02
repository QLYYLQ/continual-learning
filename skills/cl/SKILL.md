---
name: cl
description: "Continual Learning v3 - 查看系统概览、数据状态、instinct 列表、使用方法，以及设计背后的学术参考。"
---

# Continual Learning v3

## Quick Commands

Run these via bash:

```bash
# Show all instincts and data stats
python3 ~/.claude/continual-learning/cli/instinct_cli.py status

# Observer daemon management
python3 ~/.claude/continual-learning/cli/instinct_cli.py observer start
python3 ~/.claude/continual-learning/cli/instinct_cli.py observer stop
python3 ~/.claude/continual-learning/cli/instinct_cli.py observer status

# Bash insight management
python3 ~/.claude/continual-learning/cli/instinct_cli.py bash-insight list
python3 ~/.claude/continual-learning/cli/instinct_cli.py bash-insight sync
python3 ~/.claude/continual-learning/cli/instinct_cli.py bash-insight test "<command>"
python3 ~/.claude/continual-learning/cli/instinct_cli.py bash-insight add --regex "..." --action "..."

# Materialize high-confidence instincts to rules
python3 ~/.claude/continual-learning/cli/instinct_cli.py materialize

# Run one analysis cycle manually
~/.claude/continual-learning/observer/daemon.sh run

# Import/export instincts
python3 ~/.claude/continual-learning/cli/instinct_cli.py export -o backup.yaml
python3 ~/.claude/continual-learning/cli/instinct_cli.py import teammate.yaml
```

## Architecture

4-stage pipeline:
1. **Stage 1** (Python, no LLM): JSONL events → per-session JSON files
2. **Stage 2** (LLM): Sessions → task classification + registry
3. **Stage 3** (LLM): Dirty tasks → strategy/pattern instincts
4. **Stage 3b** (LLM): Dirty tasks → bash_pattern instincts with intercept rules

## Data Flow

```
User Session → Hooks (record.sh) → turns.jsonl
                                       ↓
Observer daemon (polls every N minutes)
                                       ↓
Stage 1: segment_sessions.py → sessions/{sid}.json
Stage 2: LLM task classifier → task_registry.json
Stage 3: LLM pattern detector → instincts/personal/*.yaml
Stage 3b: LLM bash analyzer → bash_pattern instincts
                                       ↓
High-confidence instincts → ~/.claude/rules/learned.md (system prompt)
bash_pattern instincts → intercept.py (runtime blocking)
```
