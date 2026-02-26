#!/usr/bin/env python3
"""
Continuous Learning v3 - Incremental Episode Extractor

Compares analyzed_episodes.json (Stage 2 output) against a state file
(.stage3_processed) to extract only unprocessed episodes for Stage 3.

Outputs incremental JSON to stdout with the same sessions/episodes structure
but containing only new episodes.

Usage:
  # Extract new episodes (dry run)
  python3 extract_new_episodes.py --analyzed data/analyzed_episodes.json --state data/.stage3_processed

  # Mark episodes as processed after successful Stage 3
  python3 extract_new_episodes.py --analyzed data/analyzed_episodes.json --state data/.stage3_processed --mark-done
"""

import argparse
import json
import sys
from pathlib import Path


def load_state(state_path: str) -> dict[str, list[int]]:
    """Load processed state: {session_id: [episode_idx, ...]}."""
    try:
        with open(state_path, "r") as f:
            data = json.load(f)
        if isinstance(data, dict) and "processed" in data:
            return data["processed"]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def load_analyzed(analyzed_path: str) -> dict:
    """Load analyzed_episodes.json."""
    with open(analyzed_path, "r") as f:
        return json.load(f)


def extract_new(analyzed: dict, processed: dict[str, list[int]]) -> dict:
    """Extract episodes not yet in the processed state."""
    new_sessions = []
    new_count = 0
    total_count = 0

    for session in analyzed.get("sessions", []):
        sid = session.get("session_id", "unknown")
        processed_indices = set(processed.get(sid, []))
        new_episodes = []

        for episode in session.get("episodes", []):
            idx = episode.get("episode_idx", -1)
            total_count += 1
            if idx not in processed_indices:
                new_episodes.append(episode)
                new_count += 1

        if new_episodes:
            new_sessions.append({
                "session_id": sid,
                "episodes": new_episodes,
            })

    return {
        "incremental": True,
        "new_episode_count": new_count,
        "total_episode_count": total_count,
        "sessions": new_sessions,
    }


def mark_done(analyzed: dict, state_path: str) -> None:
    """Update state file to mark all current episodes as processed."""
    processed: dict[str, list[int]] = {}

    for session in analyzed.get("sessions", []):
        sid = session.get("session_id", "unknown")
        indices = [ep.get("episode_idx", -1) for ep in session.get("episodes", [])]
        processed[sid] = sorted(indices)

    state = {
        "processed": processed,
        "analyzed_at": analyzed.get("analyzed_at", ""),
    }

    with open(state_path, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CL v3 Incremental Episode Extractor"
    )
    parser.add_argument(
        "--analyzed", required=True, help="Path to analyzed_episodes.json"
    )
    parser.add_argument(
        "--state", required=True, help="Path to .stage3_processed state file"
    )
    parser.add_argument(
        "--mark-done",
        action="store_true",
        help="Update state file to mark all episodes as processed",
    )
    args = parser.parse_args()

    analyzed = load_analyzed(args.analyzed)

    if args.mark_done:
        mark_done(analyzed, args.state)
        total = sum(
            len(s.get("episodes", [])) for s in analyzed.get("sessions", [])
        )
        print(f"Marked {total} episodes as processed", file=sys.stderr)
        return

    processed = load_state(args.state)
    result = extract_new(analyzed, processed)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
