#!/usr/bin/env python3
"""
Continuous Learning v4 - Trajectory Enricher

Reads the Stage 3 bundle and enriches task trajectories with:
  - Action chains extracted from Claude Code transcripts
  - Subagent summaries from agent transcript files

Input:  cache/stage3_bundle.json + transcript files on disk
Output: cache/stage3_enriched_bundle.json

Budget controls:
  - max_enriched_turns_per_task (default 30)
  - max_action_chain_blocks (default 50)
  - max_subagent_summaries (default 10)
  - max output size 512KB

Exit codes:
  0 — enriched bundle written
  2 — no bundle to enrich or input missing
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Import from sibling module
sys.path.insert(0, str(Path(__file__).parent))
from transcript_reader import (
    extract_action_chain,
    extract_subagent_summary,
    read_transcript,
)

CL_DIR = Path.home() / ".claude" / "continual-learning"
DEFAULT_CONFIG = CL_DIR / "config.json"

MAX_OUTPUT_SIZE = 512 * 1024  # 512KB


def load_config(config_path: str) -> dict:
    try:
        with open(config_path) as f:
            return json.load(f)
    except Exception:
        return {}


def load_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def find_transcript_path(sessions_dir: str, sid: str) -> str:
    """Find transcript path for a session from its session JSON."""
    session_path = os.path.join(sessions_dir, f"{sid}.json")
    try:
        with open(session_path) as f:
            session = json.load(f)
        return session.get("transcript_path", "")
    except (FileNotFoundError, json.JSONDecodeError):
        return ""


def enrich_task(task: dict, sessions_dir: str, enrichment_cfg: dict,
                max_transcript_mb: float) -> dict:
    """Enrich a single task's trajectory with action chains and subagent summaries."""
    max_turns = enrichment_cfg.get("max_enriched_turns_per_task", 30)
    max_blocks = enrichment_cfg.get("max_action_chain_blocks", 50)
    max_subagents = enrichment_cfg.get("max_subagent_summaries", 10)

    trajectory = task.get("trajectory", [])
    enriched_turns = []
    subagent_summaries = []
    subagent_count = 0
    turn_count = 0

    # Extract action chain once per session (not per turn) to avoid duplicate processing
    session_action_chains: dict[str, list[dict]] = {}

    for entry in trajectory:
        if entry.get("_session_break"):
            enriched_turns.append(entry)
            continue

        sid = entry.get("sid", "")

        # Load and extract action chain per session (once)
        if sid and sid not in session_action_chains:
            tp = find_transcript_path(sessions_dir, sid)
            if tp:
                messages = read_transcript(tp, max_transcript_mb)
                if messages:
                    chain = extract_action_chain(messages, max_blocks=max_blocks)
                    session_action_chains[sid] = chain
                else:
                    session_action_chains[sid] = []
            else:
                session_action_chains[sid] = []

        if turn_count < max_turns:
            # Attach session-level action chain to first turn of each session
            action_chain = session_action_chains.get(sid, [])
            if action_chain and not any(
                e.get("sid") == sid and "action_chain" in e
                for e in enriched_turns
                if not e.get("_session_break")
            ):
                entry = {**entry, "action_chain": action_chain}

            # Extract subagent summaries
            subagent_stops = entry.get("subagent_stops", [])
            for sa in subagent_stops:
                if subagent_count >= max_subagents:
                    break
                atp = sa.get("agent_transcript_path", "")
                if atp:
                    summary = extract_subagent_summary(atp, max_transcript_mb)
                    if summary:
                        summary["agent"] = sa.get("agent", "?")
                        summary["agent_id"] = sa.get("agent_id", "")
                        subagent_summaries.append(summary)
                        subagent_count += 1

        enriched_turns.append(entry)
        turn_count += 1

    enriched_task = {**task, "trajectory": enriched_turns}
    if subagent_summaries:
        enriched_task["subagent_summaries"] = subagent_summaries
    return enriched_task


def main() -> None:
    parser = argparse.ArgumentParser(description="CL v4 Trajectory Enricher")
    parser.add_argument("--bundle", required=True, help="Path to stage3_bundle.json")
    parser.add_argument("--sessions-dir", required=True, help="Path to sessions directory")
    parser.add_argument("--output", required=True, help="Output path for enriched bundle")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.json")
    args = parser.parse_args()

    if not os.path.exists(args.bundle):
        print("No bundle file found", file=sys.stderr)
        sys.exit(2)

    bundle = load_json(args.bundle)
    if not bundle.get("dirty_tasks"):
        print("No dirty tasks in bundle", file=sys.stderr)
        sys.exit(2)

    config = load_config(args.config)
    enrichment_cfg = config.get("enrichment", {})
    max_transcript_mb = enrichment_cfg.get("max_transcript_size_mb", 2.0)

    enriched_tasks = []
    for task in bundle["dirty_tasks"]:
        enriched = enrich_task(task, args.sessions_dir, enrichment_cfg, max_transcript_mb)
        enriched_tasks.append(enriched)

    enriched_bundle = {"dirty_tasks": enriched_tasks}

    # Check output size
    output_json = json.dumps(enriched_bundle, ensure_ascii=False, indent=2)
    if len(output_json.encode("utf-8")) > MAX_OUTPUT_SIZE:
        # Fallback: strip action chains to reduce size
        print("Warning: enriched bundle exceeds 512KB, stripping action chains",
              file=sys.stderr)
        for task in enriched_tasks:
            for entry in task.get("trajectory", []):
                entry.pop("action_chain", None)
        output_json = json.dumps(enriched_bundle, ensure_ascii=False, indent=2)

        # If still too large, fall back to basic bundle
        if len(output_json.encode("utf-8")) > MAX_OUTPUT_SIZE:
            print("Warning: still too large, falling back to basic bundle",
                  file=sys.stderr)
            with open(args.bundle) as f:
                output_json = f.read()

    with open(args.output, "w") as f:
        f.write(output_json)

    print(f"Enriched {len(enriched_tasks)} tasks", file=sys.stderr)


if __name__ == "__main__":
    main()
