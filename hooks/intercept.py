#!/usr/bin/env python3
"""
Continuous Learning v3 - Bash Intercept Script

Matches bash commands against bash_pattern instincts and blocks
known-bad patterns with exit code 2.

Reads from stdin: JSON hook payload with tool_input.command
Exits 0: allow command
Exits 2: block command (stderr contains insight message)

Supports:
  - CL_BASH_INTERCEPT=0 prefix → skip all interception for this command
  - CL_SKIP=<id> prefix → skip specific instinct
  - config.json bash_intercept.enabled toggle
"""

import json
import os
import re
import sys
import time
from pathlib import Path


CL_DIR = Path.home() / ".claude" / "continual-learning"
CONFIG_PATH = CL_DIR / "config.json"
INSTINCTS_DIR = CL_DIR / "instincts" / "personal"
COOLDOWN_FILE = CL_DIR / "data" / ".intercept_cooldown"


def load_config() -> dict:
    """Load config.json."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def parse_instinct_frontmatter(content: str) -> dict:
    """Parse YAML-like frontmatter from instinct file."""
    result = {}
    in_frontmatter = False
    in_intercept = False

    for line in content.split('\n'):
        stripped = line.strip()

        if stripped == '---':
            if in_frontmatter:
                break
            in_frontmatter = True
            continue

        if not in_frontmatter:
            continue

        # Handle nested intercept block
        if stripped.startswith('intercept:'):
            in_intercept = True
            result['intercept'] = {}
            continue

        if in_intercept:
            if stripped and not stripped.startswith('#'):
                if ':' in stripped:
                    key, value = stripped.split(':', 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    result['intercept'][key] = value
                else:
                    in_intercept = False

        if not in_intercept and ':' in line and not line.startswith(' '):
            key, value = line.split(':', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            if key in ('confidence', 'observations'):
                try:
                    result[key] = float(value)
                except ValueError:
                    result[key] = value
            else:
                result[key] = value

    return result


def extract_action_section(content: str) -> str:
    """Extract the ## Action section from instinct content."""
    match = re.search(r'## Action\s*\n(.+?)(?:\n## |\Z)', content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""


def load_bash_instincts() -> list[dict]:
    """Load all bash_pattern instincts, sorted by confidence desc."""
    instincts = []

    if not INSTINCTS_DIR.exists():
        return instincts

    for filepath in INSTINCTS_DIR.glob("*.yaml"):
        try:
            content = filepath.read_text()
            meta = parse_instinct_frontmatter(content)

            if meta.get('type') != 'bash_pattern':
                continue

            if 'intercept' not in meta or 'regex' not in meta.get('intercept', {}):
                continue

            meta['_content'] = content
            meta['_action'] = extract_action_section(content)
            meta['_filepath'] = str(filepath)
            instincts.append(meta)
        except Exception:
            continue

    instincts.sort(key=lambda x: -x.get('confidence', 0))
    return instincts


def check_cooldown(config: dict) -> bool:
    """Check if we're within cooldown period."""
    cooldown_secs = config.get('bash_intercept', {}).get('cooldown_seconds', 5)
    if cooldown_secs <= 0:
        return False

    try:
        if COOLDOWN_FILE.exists():
            last_block = float(COOLDOWN_FILE.read_text().strip())
            if time.time() - last_block < cooldown_secs:
                return True
    except Exception:
        pass

    return False


def set_cooldown():
    """Record cooldown timestamp."""
    try:
        COOLDOWN_FILE.write_text(str(time.time()))
    except Exception:
        pass


def main():
    # Read hook payload from stdin
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    # Extract command
    tool_input = payload.get('tool_input', {})
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except Exception:
            tool_input = {}

    command = tool_input.get('command', '')
    if not command:
        sys.exit(0)

    # Load config
    config = load_config()
    intercept_cfg = config.get('bash_intercept', {})

    # Global toggle: config.json
    if not intercept_cfg.get('enabled', True):
        sys.exit(0)

    # Per-command toggle: CL_BASH_INTERCEPT=0 prefix
    if command.startswith('CL_BASH_INTERCEPT=0 ') or command.startswith('CL_BASH_INTERCEPT=0\t'):
        sys.exit(0)

    # Check cooldown
    if check_cooldown(config):
        sys.exit(0)

    # Thresholds
    block_threshold = intercept_cfg.get('block_threshold', 0.7)
    warn_threshold = intercept_cfg.get('warn_threshold', 0.5)

    # Load bash_pattern instincts
    instincts = load_bash_instincts()
    if not instincts:
        sys.exit(0)

    # Check each instinct's regex against the command
    for inst in instincts:
        confidence = inst.get('confidence', 0)

        # Skip below warn threshold
        if confidence < warn_threshold:
            continue

        inst_id = inst.get('id', 'unknown')
        intercept_meta = inst.get('intercept', {})
        regex_str = intercept_meta.get('regex', '')
        bypass_env = intercept_meta.get('bypass_env', '')

        if not regex_str:
            continue

        # Check CL_SKIP=<id> bypass
        skip_pattern = f'CL_SKIP={inst_id} '
        if command.startswith(skip_pattern) or f' CL_SKIP={inst_id} ' in command:
            continue

        # Check specific bypass env prefix
        if bypass_env:
            bypass_pattern = f'{bypass_env}=1 '
            if command.startswith(bypass_pattern) or f' {bypass_pattern}' in command:
                continue

        # Match regex against command
        try:
            if re.search(regex_str, command):
                trigger = inst.get('trigger', 'Unknown pattern')
                action = inst.get('_action', 'No action specified')

                if confidence >= block_threshold:
                    # BLOCK: exit 2
                    set_cooldown()
                    msg = (
                        f"\n[CL Bash Insight] {trigger}\n"
                        f"Confidence: {confidence:.2f} | ID: {inst_id}\n"
                        f"{action}\n\n"
                        f"To bypass: prepend CL_SKIP={inst_id} to your command\n"
                    )
                    if bypass_env:
                        msg += f"Or prepend: {bypass_env}=1\n"
                    msg += "To disable all: prepend CL_BASH_INTERCEPT=0\n"

                    print(msg, file=sys.stderr)
                    sys.exit(2)
                else:
                    # WARN: just print to stderr, don't block
                    msg = (
                        f"\n[CL Bash Warning] {trigger}\n"
                        f"Confidence: {confidence:.2f} (below block threshold {block_threshold})\n"
                        f"{action}\n"
                    )
                    print(msg, file=sys.stderr)
                    # Don't exit 2 for warnings
        except re.error:
            # Invalid regex, skip
            continue

    sys.exit(0)


if __name__ == '__main__':
    main()
