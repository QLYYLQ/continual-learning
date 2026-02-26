#!/usr/bin/env python3
"""
Continuous Learning v3 - Bash Context Extractor (Stage 3b pre-processing)

Pure Python, no LLM. Extracts bash command context elements from turns.jsonl
into a JSONL file. Each element is a self-contained context window around a
bash call, suitable for LLM analysis.

Context window per element:
  - 2 events before the bash call (the "why")
  - The bash call itself (e:"tool", tool:"Bash")
  - Feedback: the next bash_ok or fail event for Bash
  - 1 event after feedback (catches corrections, pivots)

Input:  data/turns.jsonl + data/.stage3b_processed (state)
Output: data/.stage3b_contexts.jsonl

Usage:
  # Extract contexts (only new/changed sessions)
  python3 extract_bash_contexts.py --input data/turns.jsonl \
      --state data/.stage3b_processed --output data/.stage3b_contexts.jsonl

  # Mark all sessions as processed
  python3 extract_bash_contexts.py --input data/turns.jsonl \
      --state data/.stage3b_processed --mark-done
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def parse_jsonl(path: str) -> list[dict]:
    """Read JSONL file, skip malformed lines."""
    events = []
    with open(path, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
                if evt.get("v") == 3:
                    events.append(evt)
            except json.JSONDecodeError:
                print(
                    f"Warning: skipping malformed line {line_num}",
                    file=sys.stderr,
                )
    return events


def group_by_session(events: list[dict]) -> dict[str, list[dict]]:
    """Group events by session_id, preserving order."""
    sessions: dict[str, list[dict]] = defaultdict(list)
    for evt in events:
        sid = evt.get("sid", "unknown")
        sessions[sid].append(evt)
    return dict(sessions)


def load_state(state_path: str) -> dict:
    """Load processed state: {session_id: {event_count, analyzed_at}}."""
    try:
        with open(state_path, "r") as f:
            data = json.load(f)
        if isinstance(data, dict) and "processed_sessions" in data:
            return data["processed_sessions"]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def save_state(state_path: str, sessions: dict[str, list[dict]]) -> None:
    """Save processed state with event counts per session."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    processed = {}
    for sid, events in sessions.items():
        processed[sid] = {
            "event_count": len(events),
            "analyzed_at": now,
        }
    state = {"processed_sessions": processed}
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def is_bash_tool_call(evt: dict) -> bool:
    """Check if event is a Bash tool call."""
    return evt.get("e") == "tool" and evt.get("tool") == "Bash"


def is_bash_feedback(evt: dict) -> bool:
    """Check if event is bash feedback (bash_ok or fail for Bash)."""
    e_type = evt.get("e")
    if e_type == "bash_ok":
        return True
    if e_type == "fail" and evt.get("tool") == "Bash":
        return True
    return False


def slim_event(evt: dict) -> dict:
    """Create a slim copy of an event for context output.

    Keeps essential fields, drops v (version) to save space.
    Truncates long string values.
    """
    slim = {}
    max_str_len = 500

    for key, val in evt.items():
        if key == "v":
            continue
        if isinstance(val, str) and len(val) > max_str_len:
            slim[key] = val[:max_str_len] + "..."
        else:
            slim[key] = val
    return slim


def extract_contexts(session_events: list[dict]) -> list[dict]:
    """Extract bash context elements from a session's events.

    For each Bash tool call, captures:
      - context_before: up to 2 preceding events
      - bash_call: the Bash tool event
      - feedback: the next bash_ok or fail event
      - context_after: up to 1 event after feedback
    """
    contexts = []
    idx = 0

    for i, evt in enumerate(session_events):
        if not is_bash_tool_call(evt):
            continue

        # Context before: up to 2 preceding events
        context_before = []
        start = max(0, i - 2)
        for j in range(start, i):
            context_before.append(slim_event(session_events[j]))

        # The bash call itself
        bash_call = slim_event(evt)

        # Find feedback: next bash_ok or fail event after this bash call
        feedback = None
        feedback_idx = None
        for j in range(i + 1, len(session_events)):
            if is_bash_feedback(session_events[j]):
                feedback = slim_event(session_events[j])
                feedback_idx = j
                break
            # Stop searching if we hit another tool call without feedback
            if session_events[j].get("e") == "tool":
                break

        # Context after: 1 event after feedback
        context_after = []
        if feedback_idx is not None:
            after_start = feedback_idx + 1
            if after_start < len(session_events):
                context_after.append(slim_event(session_events[after_start]))

        # Determine failure and correction candidate status
        has_failure = (
            feedback is not None and feedback.get("e") == "fail"
        )

        correction_candidate = False
        if has_failure:
            correction_candidate = True
        elif context_after:
            # If the next event after feedback is a different Bash command
            after_evt = context_after[0]
            if (
                after_evt.get("e") == "tool"
                and after_evt.get("tool") == "Bash"
            ):
                after_cmd = after_evt.get("command", "")
                bash_cmd = evt.get("command", "")
                if after_cmd != bash_cmd:
                    correction_candidate = True

        context = {
            "idx": idx,
            "context_before": context_before,
            "bash_call": bash_call,
            "feedback": feedback,
            "context_after": context_after,
            "has_failure": has_failure,
            "correction_candidate": correction_candidate,
        }

        contexts.append(context)
        idx += 1

    return contexts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CL v3 Bash Context Extractor (Stage 3b)"
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to turns.jsonl",
    )
    parser.add_argument(
        "--state",
        required=True,
        help="Path to .stage3b_processed state file",
    )
    parser.add_argument(
        "--output",
        help="Path to output JSONL file (required unless --mark-done)",
    )
    parser.add_argument(
        "--mark-done",
        action="store_true",
        help="Update state file to mark all sessions as processed",
    )
    args = parser.parse_args()

    # Parse all events
    events = parse_jsonl(args.input)
    if not events:
        if args.output:
            Path(args.output).write_text("")
        print("No events found", file=sys.stderr)
        return

    sessions = group_by_session(events)

    if args.mark_done:
        save_state(args.state, sessions)
        total = sum(len(evts) for evts in sessions.values())
        print(
            f"Marked {len(sessions)} sessions ({total} events) as processed",
            file=sys.stderr,
        )
        return

    if not args.output:
        print("--output is required unless --mark-done is used", file=sys.stderr)
        sys.exit(1)

    # Load state to find new/changed sessions
    processed = load_state(args.state)

    # Find sessions that need processing
    new_sessions = {}
    for sid, session_events in sessions.items():
        prev = processed.get(sid)
        if prev is None:
            # Never processed
            new_sessions[sid] = session_events
        elif len(session_events) > prev.get("event_count", 0):
            # Session has grown since last processing
            new_sessions[sid] = session_events

    if not new_sessions:
        Path(args.output).write_text("")
        print("No new sessions to process", file=sys.stderr)
        return

    # Extract bash contexts from new sessions
    total_contexts = 0
    with open(args.output, "w") as f:
        for sid, session_events in new_sessions.items():
            contexts = extract_contexts(session_events)
            for ctx in contexts:
                ctx["sid"] = sid
                f.write(json.dumps(ctx, ensure_ascii=False) + "\n")
                total_contexts += 1

    print(
        f"Extracted {total_contexts} bash contexts from "
        f"{len(new_sessions)} sessions",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
