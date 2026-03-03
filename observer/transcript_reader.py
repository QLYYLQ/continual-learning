#!/usr/bin/env python3
"""
Continuous Learning v4 - Transcript Reader

Reads Claude Code transcript JSONL files and extracts structured data
for Stage 3 enrichment.

Three core functions:
  read_transcript(path)          - Parse transcript JSONL
  extract_action_chain(messages)  - Extract ordered action chain
  extract_subagent_summary(path)  - Summarize a subagent transcript
"""

import json
import os
from typing import Any


def read_transcript(path: str, max_size_mb: float = 2.0) -> list[dict]:
    """Read a Claude Code transcript JSONL file.

    Returns list of message dicts. Skips files exceeding max_size_mb.
    """
    if not path or not os.path.exists(path):
        return []

    file_size = os.path.getsize(path)
    if file_size > max_size_mb * 1024 * 1024:
        return []

    messages = []
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    # Claude Code transcripts wrap messages in a top-level
                    # object with keys like type, message, uuid, etc.
                    # Extract the inner message dict if present.
                    msg = entry.get("message", entry) if isinstance(entry, dict) else entry
                    if isinstance(msg, dict) and msg.get("role"):
                        messages.append(msg)
                except json.JSONDecodeError:
                    continue
    except (OSError, PermissionError):
        return []

    return messages


def extract_action_chain(messages: list[dict], max_blocks: int = 50) -> list[dict]:
    """Extract an ordered action chain from transcript messages.

    Returns a list of action blocks, each describing one step in the
    assistant's reasoning/tool-use chain:
      - {"type": "thinking", "text": "..."}
      - {"type": "text", "text": "..."}
      - {"type": "tool_use", "tool": "...", "input_summary": "..."}
      - {"type": "tool_result", "tool": "...", "output_summary": "..."}

    Truncates individual text fields to keep total size manageable.
    """
    chain: list[dict] = []
    block_count = 0

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "assistant":
            if isinstance(content, str):
                if content.strip():
                    chain.append({
                        "type": "text",
                        "text": content[:500],
                    })
                    block_count += 1
            elif isinstance(content, list):
                for block in content:
                    if block_count >= max_blocks:
                        break
                    btype = block.get("type", "")

                    if btype == "thinking":
                        text = block.get("thinking", "")
                        if text:
                            chain.append({
                                "type": "thinking",
                                "text": text[:300],
                            })
                            block_count += 1

                    elif btype == "text":
                        text = block.get("text", "")
                        if text:
                            chain.append({
                                "type": "text",
                                "text": text[:500],
                            })
                            block_count += 1

                    elif btype == "tool_use":
                        tool_name = block.get("name", "?")
                        tool_input = block.get("input", {})
                        tool_use_id = block.get("id", "")
                        input_summary = _summarize_tool_input(tool_name, tool_input)
                        chain.append({
                            "type": "tool_use",
                            "tool": tool_name,
                            "tool_use_id": tool_use_id,
                            "input_summary": input_summary,
                        })
                        block_count += 1

        elif role == "user":
            if isinstance(content, list):
                for block in content:
                    if block_count >= max_blocks:
                        break
                    btype = block.get("type", "")

                    if btype == "tool_result":
                        tool_use_id = block.get("tool_use_id", "")
                        result_content = block.get("content", "")
                        output_summary = _summarize_tool_output(result_content)
                        # Try to find the tool name from a previous tool_use
                        tool_name = _find_tool_name(chain, tool_use_id)
                        chain.append({
                            "type": "tool_result",
                            "tool": tool_name,
                            "output_summary": output_summary,
                        })
                        block_count += 1

        if block_count >= max_blocks:
            break

    return chain


def extract_subagent_summary(agent_transcript_path: str,
                              max_size_mb: float = 2.0) -> dict | None:
    """Read a subagent transcript and return a summary.

    Returns:
      {
        "turn_count": int,
        "tool_calls": [{"tool": str, "count": int}, ...],
        "failures": int,
        "final_response": str (truncated),
      }
    """
    messages = read_transcript(agent_transcript_path, max_size_mb)
    if not messages:
        return None

    tool_counts: dict[str, int] = {}
    failures = 0
    final_response = ""
    turn_count = 0

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "user":
            turn_count += 1
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result":
                        is_error = block.get("is_error", False)
                        if is_error:
                            failures += 1

        elif role == "assistant":
            if isinstance(content, str):
                if content.strip():
                    final_response = content
            elif isinstance(content, list):
                text_parts = []
                for block in content:
                    btype = block.get("type", "")
                    if btype == "tool_use":
                        tool_name = block.get("name", "?")
                        tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
                    elif btype == "text":
                        text = block.get("text", "")
                        if text:
                            text_parts.append(text)
                if text_parts:
                    final_response = "\n".join(text_parts)

    tool_calls = [{"tool": k, "count": v} for k, v in sorted(tool_counts.items())]

    return {
        "turn_count": turn_count,
        "tool_calls": tool_calls,
        "failures": failures,
        "final_response": final_response[:1000],
    }


def _summarize_tool_input(tool_name: str, tool_input: dict) -> str:
    """Create a concise summary of tool input."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:200] if cmd else ""
    elif tool_name == "Read":
        return tool_input.get("file_path", "")[:200]
    elif tool_name == "Write":
        path = tool_input.get("file_path", "")
        return f"write {path}"[:200]
    elif tool_name == "Edit":
        path = tool_input.get("file_path", "")
        return f"edit {path}"[:200]
    elif tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"grep '{pattern}' in {path}"[:200]
    elif tool_name == "Glob":
        return tool_input.get("pattern", "")[:200]
    elif tool_name in ("Agent", "Task"):
        agent = tool_input.get("subagent_type", "?")
        prompt = tool_input.get("prompt", "")[:100]
        return f"{agent}: {prompt}"
    else:
        # Generic: show first key-value pair
        for k, v in tool_input.items():
            return f"{k}={str(v)[:100]}"
        return ""


def _summarize_tool_output(content: Any) -> str:
    """Create a concise summary of tool output."""
    if isinstance(content, str):
        return content[:300]
    elif isinstance(content, list):
        # Flatten text blocks
        texts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text", "")
                if text:
                    texts.append(text[:150])
        return " | ".join(texts)[:300]
    return str(content)[:300]


def _find_tool_name(chain: list[dict], tool_use_id: str) -> str:
    """Look back in chain for matching tool_use to get tool name."""
    # Try matching by tool_use_id first
    if tool_use_id:
        for block in chain:
            if block.get("type") == "tool_use" and block.get("tool_use_id") == tool_use_id:
                return block.get("tool", "?")
    # Fallback: return the most recent tool_use's tool name
    for block in reversed(chain):
        if block.get("type") == "tool_use":
            return block.get("tool", "?")
    return "?"
