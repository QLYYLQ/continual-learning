#!/usr/bin/env python3
"""
Continuous Learning v3 - Session Segmenter (New Stage 1)

Pure Python, no LLM. Converts raw JSONL events into per-session JSON files
with lightweight signals for task classification.

Input:  data/turns.jsonl
Output: data/sessions/{sid}.json + data/sessions/_index.json
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CL_DIR = Path.home() / ".claude" / "continual-learning"
DEFAULT_INPUT = CL_DIR / "data" / "turns.jsonl"
DEFAULT_OUTDIR = CL_DIR / "data" / "sessions"
DEFAULT_INDEX = DEFAULT_OUTDIR / "_index.json"
DEFAULT_CONFIG = CL_DIR / "config.json"

# Stopwords for prompt keyword extraction.
# Used by extract_prompt_keywords() to filter out common function words
# so the top-10 keywords are meaningful content words (e.g. "observer",
# "pipeline", "stage3") rather than noise ("the", "is", "的", "了").
# Stage 2 LLM uses these keywords to detect cross-session task similarity.
STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "out", "off", "over",
    "under", "again", "further", "then", "once", "here", "there", "when",
    "where", "why", "how", "all", "each", "every", "both", "few", "more",
    "most", "other", "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "just", "because", "but", "and",
    "or", "if", "while", "about", "up", "it", "its", "this", "that",
    "these", "those", "i", "me", "my", "we", "our", "you", "your",
    "he", "him", "his", "she", "her", "they", "them", "their", "what",
    "which", "who", "whom", "let", "also", "like", "use", "using",
    "make", "get", "see", "now", "ok", "please", "need", "want",
    # Chinese particles / function words
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都",
    "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你",
    "会", "着", "没有", "看", "好", "自己", "这", "他", "她", "它",
    "把", "那", "里", "吗", "吧", "呢", "啊", "给", "让", "下",
}

# Regex to tokenize: splits on non-alphanumeric/non-CJK characters
TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff\u3400-\u4dbf]+", re.UNICODE)


def parse_jsonl(path: str) -> list[dict]:
    """Read JSONL file, skip malformed lines. Accepts v3 and v4 events."""
    events = []
    with open(path, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
                if evt.get("v") in (3, 4):
                    events.append(evt)
            except json.JSONDecodeError:
                print(f"Warning: skipping malformed line {line_num}", file=sys.stderr)
    return events


def group_by_session(events: list[dict]) -> dict[str, list[dict]]:
    """Group events by session_id, preserving order."""
    sessions: dict[str, list[dict]] = defaultdict(list)
    for evt in events:
        sid = evt.get("sid", "unknown")
        sessions[sid].append(evt)
    return dict(sessions)


def build_turns(session_events: list[dict]) -> list[dict]:
    """
    Split a session's events into Turns.

    A Turn starts with a 'turn' event (user prompt) and includes all
    subsequent tool/delegate/skill/bash_ok/fail/stop events until the
    next 'turn' event.
    """
    turns: list[dict] = []
    current_turn: dict[str, Any] | None = None

    def finalize_turn(turn: dict) -> dict:
        """Compute statistics for a completed Turn."""
        events = turn.pop("_events", [])

        tool_counts: dict[str, int] = defaultdict(int)
        fail_count = 0
        delegates: list[dict] = []
        files_touched: set[str] = set()
        bash_commands: list[str] = []
        subagent_starts: list[dict] = []
        subagent_stops: list[dict] = []

        for evt in events:
            e_type = evt.get("e")

            if e_type == "tool":
                tool_name = evt.get("tool", "?")
                tool_counts[tool_name] += 1
                target = evt.get("target", "")
                if target and ("/" in target or "." in target):
                    files_touched.add(target)

            elif e_type == "delegate":
                delegates.append({
                    "agent": evt.get("agent", "?"),
                    "prompt_preview": evt.get("agent_prompt", "")[:200],
                })
                tool_counts["Task"] += 1

            elif e_type == "skill":
                tool_counts["Skill"] += 1

            elif e_type == "bash_ok":
                cmd = evt.get("cmd", "")
                if cmd:
                    bash_commands.append(cmd)

            elif e_type == "fail":
                fail_count += 1
                tool_name = evt.get("tool", "?")
                tool_counts[tool_name] += 1
                if tool_name == "Bash":
                    cmd = evt.get("cmd", evt.get("command", ""))
                    if cmd:
                        bash_commands.append(cmd)

            elif e_type == "agent_start":
                subagent_starts.append({
                    "agent": evt.get("agent", "?"),
                    "agent_id": evt.get("agent_id", ""),
                })

            elif e_type == "agent_stop":
                entry = {
                    "agent": evt.get("agent", "?"),
                    "agent_id": evt.get("agent_id", ""),
                }
                atp = evt.get("atp", "")
                if atp:
                    entry["agent_transcript_path"] = atp
                subagent_stops.append(entry)

        # Calculate duration
        duration_ms = 0
        if events:
            try:
                first_ts = datetime.fromisoformat(turn["ts"].replace("Z", "+00:00"))
                last_ts_str = events[-1].get("ts", turn["ts"])
                last_ts = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
                duration_ms = int((last_ts - first_ts).total_seconds() * 1000)
            except (ValueError, TypeError):
                pass

        turn["tools"] = dict(tool_counts)
        turn["files_touched"] = sorted(files_touched)
        turn["delegates"] = delegates
        turn["bash_commands"] = bash_commands
        turn["fail_count"] = fail_count
        turn["duration_ms"] = duration_ms
        if subagent_starts:
            turn["subagent_starts"] = subagent_starts
        if subagent_stops:
            turn["subagent_stops"] = subagent_stops

        return turn

    for evt in session_events:
        e_type = evt.get("e")

        if e_type == "turn":
            if current_turn is not None:
                turns.append(finalize_turn(current_turn))
            current_turn = {
                "turn_idx": len(turns),
                "ts": evt.get("ts", ""),
                "prompt": evt.get("prompt", ""),
                "cwd": evt.get("cwd", ""),
                "_events": [],
            }
        elif current_turn is not None:
            current_turn["_events"].append(evt)
        else:
            current_turn = {
                "turn_idx": len(turns),
                "ts": evt.get("ts", ""),
                "prompt": "(implicit - no user prompt recorded)",
                "cwd": "",
                "_events": [evt],
            }

    if current_turn is not None:
        turns.append(finalize_turn(current_turn))

    return turns


def extract_prompt_keywords(turns: list[dict], top_n: int = 10) -> list[str]:
    """Extract top-N content keywords from turn prompts via simple TF scoring.

    Filters out stopwords (EN/CN) so the result captures meaningful terms
    like project names, tool names, and domain concepts.
    """
    counter: Counter[str] = Counter()
    for turn in turns:
        prompt = turn.get("prompt", "")
        tokens = TOKEN_RE.findall(prompt.lower())
        for token in tokens:
            if token not in STOPWORDS and len(token) > 1:
                counter[token] += 1
    return [word for word, _ in counter.most_common(top_n)]


def extract_signals(turns: list[dict], time_gap_minutes: int = 30) -> dict:
    """Extract lightweight signals from turns for task classification."""
    cwd_switches: list[dict] = []
    time_gaps: list[dict] = []

    prev_cwd = None
    prev_ts = None

    for turn in turns:
        cwd = turn.get("cwd", "")
        ts_str = turn.get("ts", "")

        # CWD switches
        if prev_cwd is not None and cwd and cwd != prev_cwd:
            cwd_switches.append({
                "at_turn": turn["turn_idx"],
                "from": prev_cwd,
                "to": cwd,
            })

        # Time gaps
        if prev_ts is not None and ts_str:
            try:
                curr_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                gap = (curr_ts - prev_ts).total_seconds() / 60.0
                if gap > time_gap_minutes:
                    time_gaps.append({
                        "after_turn": turn["turn_idx"] - 1,
                        "gap_minutes": round(gap, 1),
                    })
            except (ValueError, TypeError):
                pass

        if cwd:
            prev_cwd = cwd
        if ts_str:
            try:
                prev_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

    prompt_keywords = extract_prompt_keywords(turns)

    return {
        "cwd_switches": cwd_switches,
        "time_gaps": time_gaps,
        "prompt_keywords": prompt_keywords,
        "session_adjacency": {
            "prev_sid": None,
            "prev_gap_minutes": None,
            "next_sid": None,
            "next_gap_minutes": None,
        },
    }


def compute_adjacency(sorted_sessions: list[dict]) -> None:
    """Second pass: fill session_adjacency for each session."""
    for i, session in enumerate(sorted_sessions):
        signals = session.get("signals", {})
        adj = signals.get("session_adjacency", {})

        if i > 0:
            prev = sorted_sessions[i - 1]
            adj["prev_sid"] = prev["session_id"]
            try:
                curr_start = datetime.fromisoformat(
                    session["time_range"]["start"].replace("Z", "+00:00")
                )
                prev_end = datetime.fromisoformat(
                    prev["time_range"]["end"].replace("Z", "+00:00")
                )
                adj["prev_gap_minutes"] = round(
                    (curr_start - prev_end).total_seconds() / 60.0, 1
                )
            except (ValueError, TypeError, KeyError):
                adj["prev_gap_minutes"] = None

        if i < len(sorted_sessions) - 1:
            nxt = sorted_sessions[i + 1]
            adj["next_sid"] = nxt["session_id"]
            try:
                curr_end = datetime.fromisoformat(
                    session["time_range"]["end"].replace("Z", "+00:00")
                )
                next_start = datetime.fromisoformat(
                    nxt["time_range"]["start"].replace("Z", "+00:00")
                )
                adj["next_gap_minutes"] = round(
                    (next_start - curr_end).total_seconds() / 60.0, 1
                )
            except (ValueError, TypeError, KeyError):
                adj["next_gap_minutes"] = None

        signals["session_adjacency"] = adj


def build_session(sid: str, session_events: list[dict], time_gap_minutes: int) -> dict:
    """Build a single session object from raw events."""
    turns = build_turns(session_events)
    signals = extract_signals(turns, time_gap_minutes)

    # Compute primary_cwd (most common non-empty cwd)
    cwd_counter: Counter[str] = Counter()
    all_cwds: set[str] = set()
    for turn in turns:
        cwd = turn.get("cwd", "")
        if cwd:
            cwd_counter[cwd] += 1
            all_cwds.add(cwd)

    primary_cwd = cwd_counter.most_common(1)[0][0] if cwd_counter else ""

    # Time range from all events (more accurate than just turn timestamps)
    all_ts = [evt.get("ts", "") for evt in session_events if evt.get("ts")]
    if all_ts:
        time_start = min(all_ts)
        time_end = max(all_ts)
    else:
        time_start = ""
        time_end = ""

    has_stop = any(evt.get("e") == "stop" for evt in session_events)

    # Extract transcript_path from first event that has it
    transcript_path = ""
    for evt in session_events:
        tp = evt.get("tp", "")
        if tp:
            transcript_path = tp
            break

    session = {
        "session_id": sid,
        "time_range": {"start": time_start, "end": time_end},
        "primary_cwd": primary_cwd,
        "all_cwds": sorted(all_cwds),
        "turn_count": len(turns),
        "event_count": len(session_events),
        "has_stop": has_stop,
        "signals": signals,
        "turns": turns,
    }
    if transcript_path:
        session["transcript_path"] = transcript_path
    return session


def segment(input_path: str, outdir: str, index_path: str, time_gap_minutes: int) -> dict:
    """Main segmentation pipeline: JSONL -> per-session files + index."""
    events = parse_jsonl(input_path)

    if not events:
        os.makedirs(outdir, exist_ok=True)
        index = {
            "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "total_sessions": 0,
            "total_turns": 0,
            "sessions": [],
        }
        with open(index_path, "w") as f:
            json.dump(index, f, indent=2, ensure_ascii=False)
        return index

    sessions_raw = group_by_session(events)
    os.makedirs(outdir, exist_ok=True)

    # Build all sessions
    sessions = []
    for sid, session_events in sessions_raw.items():
        session = build_session(sid, session_events, time_gap_minutes)
        sessions.append(session)

    # Sort chronologically by start time
    sessions.sort(key=lambda s: s["time_range"]["start"] or "")

    # Second pass: compute adjacency
    compute_adjacency(sessions)

    # Write individual session files
    for session in sessions:
        sid = session["session_id"]
        session_path = os.path.join(outdir, f"{sid}.json")
        with open(session_path, "w") as f:
            json.dump(session, f, indent=2, ensure_ascii=False)

    # Build and write index
    total_turns = sum(s["turn_count"] for s in sessions)
    index_entries = []
    for s in sessions:
        entry = {
            "sid": s["session_id"],
            "start": s["time_range"]["start"],
            "end": s["time_range"]["end"],
            "primary_cwd": s["primary_cwd"],
            "turn_count": s["turn_count"],
            "event_count": s["event_count"],
            "has_stop": s["has_stop"],
        }
        tp = s.get("transcript_path", "")
        if tp:
            entry["transcript_path"] = tp
        index_entries.append(entry)

    index = {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_sessions": len(sessions),
        "total_turns": total_turns,
        "sessions": index_entries,
    }

    with open(index_path, "w") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

    print(f"Segmented {total_turns} turns across {len(sessions)} sessions")
    return index


def load_config_gap_minutes(config_path: str) -> int:
    """Load time_gap_minutes from config, default 30."""
    try:
        with open(config_path, "r") as f:
            cfg = json.load(f)
        return int(cfg.get("observer", {}).get("time_gap_minutes", 30))
    except Exception:
        return 30


def main() -> None:
    parser = argparse.ArgumentParser(description="CL v3 Session Segmenter (Stage 1)")
    parser.add_argument("--input", "-i", default=str(DEFAULT_INPUT),
                        help="Input JSONL file path")
    parser.add_argument("--outdir", "-d", default=str(DEFAULT_OUTDIR),
                        help="Output directory for session files")
    parser.add_argument("--index", default=str(DEFAULT_INDEX),
                        help="Output path for _index.json")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="Path to config.json")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    time_gap_minutes = load_config_gap_minutes(args.config)
    segment(args.input, args.outdir, args.index, time_gap_minutes)


if __name__ == "__main__":
    main()
