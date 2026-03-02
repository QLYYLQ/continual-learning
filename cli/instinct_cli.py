#!/usr/bin/env python3
"""
Continuous Learning v3 - Instinct CLI

Commands:
  status      Show all instincts and their status
  import      Import instincts from file or URL
  export      Export instincts to file
  evolve      Cluster instincts into skills/commands/agents
  observer    Manage observer daemon (start/stop/status)
  materialize Generate rules/prompts from high-confidence instincts
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

CL_DIR = Path.home() / ".claude" / "continual-learning"
INSTINCTS_DIR = CL_DIR / "instincts"
PERSONAL_DIR = INSTINCTS_DIR / "personal"
INHERITED_DIR = INSTINCTS_DIR / "inherited"
EVOLVED_DIR = CL_DIR / "evolved"
DATA_DIR = CL_DIR / "data"
CONFIG_FILE = CL_DIR / "config.json"
DAEMON_SCRIPT = CL_DIR / "observer" / "daemon.sh"
RULES_FILE = Path.home() / ".claude" / "rules" / "learned.md"
PROMPTS_DIR = CL_DIR / "prompts"

# Ensure directories exist
for d in [PERSONAL_DIR, INHERITED_DIR, EVOLVED_DIR / "skills",
          EVOLVED_DIR / "commands", EVOLVED_DIR / "agents", PROMPTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    """Load config.json."""
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


# ─────────────────────────────────────────────
# Instinct Parser
# ─────────────────────────────────────────────

def parse_instinct_file(content: str) -> list[dict]:
    """Parse YAML-like instinct file format.

    Handles:
    - Standard frontmatter between --- delimiters
    - Nested YAML blocks (e.g., intercept: with indented sub-keys)
    - Content after closing --- (## Pattern, ## Action, ## Evidence)
    - Multiple instincts in a single file
    """
    instincts = []
    current = {}
    in_frontmatter = False
    content_lines = []
    nested_key = None  # Track nested YAML block (e.g., 'intercept')

    for line in content.split('\n'):
        if line.strip() == '---':
            if in_frontmatter:
                # Closing ---: end frontmatter, start collecting content
                in_frontmatter = False
                nested_key = None
                content_lines = []
            else:
                # Opening ---: finalize previous instinct if any, start new frontmatter
                in_frontmatter = True
                nested_key = None
                if current:
                    current['content'] = '\n'.join(content_lines).strip()
                    instincts.append(current)
                current = {}
                content_lines = []
        elif in_frontmatter:
            if ':' in line:
                # Check if this is a nested sub-key (indented)
                if line[0] in (' ', '\t') and nested_key:
                    # Sub-key of nested block
                    key, value = line.strip().split(':', 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if isinstance(current.get(nested_key), dict):
                        current[nested_key][key] = value
                else:
                    # Top-level key
                    key, value = line.split(':', 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")

                    if key in ('confidence', 'observations'):
                        try:
                            current[key] = float(value)
                        except ValueError:
                            current[key] = value
                    elif not value:
                        # Empty value = start of nested block
                        current[key] = {}
                        nested_key = key
                    else:
                        current[key] = value
                        nested_key = None
        else:
            content_lines.append(line)

    # Finalize last instinct
    if current:
        current['content'] = '\n'.join(content_lines).strip()
        instincts.append(current)

    return [i for i in instincts if i.get('id')]


def load_all_instincts() -> list[dict]:
    """Load all instincts from personal and inherited directories."""
    instincts = []
    for directory in [PERSONAL_DIR, INHERITED_DIR]:
        if not directory.exists():
            continue
        for file in sorted(directory.glob("*.yaml")):
            try:
                content = file.read_text()
                parsed = parse_instinct_file(content)
                for inst in parsed:
                    inst['_source_file'] = str(file)
                    inst['_source_type'] = directory.name
                instincts.extend(parsed)
            except Exception as e:
                print(f"Warning: Failed to parse {file}: {e}", file=sys.stderr)
    return instincts


# ─────────────────────────────────────────────
# Status Command
# ─────────────────────────────────────────────

def cmd_status(args):
    """Show status of all instincts."""
    instincts = load_all_instincts()

    if not instincts:
        print("No instincts found.")
        print(f"\nInstinct directories:")
        print(f"  Personal:  {PERSONAL_DIR}")
        print(f"  Inherited: {INHERITED_DIR}")
        # Show data stats if available
        _print_data_stats()
        return 0

    # Group by type
    by_type = defaultdict(list)
    for inst in instincts:
        inst_type = inst.get('type', inst.get('domain', 'general'))
        by_type[inst_type].append(inst)

    # Header
    print(f"\n{'='*60}")
    print(f"  INSTINCT STATUS - {len(instincts)} total")
    print(f"{'='*60}\n")

    # Summary by source
    personal = [i for i in instincts if i.get('_source_type') == 'personal']
    inherited = [i for i in instincts if i.get('_source_type') == 'inherited']
    print(f"  Personal:  {len(personal)}")
    print(f"  Inherited: {len(inherited)}")

    # Summary by confidence
    high = [i for i in instincts if i.get('confidence', 0) >= 0.7]
    med = [i for i in instincts if 0.3 <= i.get('confidence', 0) < 0.7]
    low = [i for i in instincts if i.get('confidence', 0) < 0.3]
    print(f"\n  High (>=0.7): {len(high)}  |  Medium: {len(med)}  |  Low (<0.3): {len(low)}")
    print()

    # Print by type
    for inst_type in sorted(by_type.keys()):
        type_instincts = by_type[inst_type]
        print(f"## {inst_type.upper().replace('_', ' ')} ({len(type_instincts)})")
        print()

        for inst in sorted(type_instincts, key=lambda x: -x.get('confidence', 0.5)):
            conf = inst.get('confidence', 0.5)
            conf_bar = '\u2588' * int(conf * 10) + '\u2591' * (10 - int(conf * 10))
            trigger = inst.get('trigger', 'unknown trigger')
            obs = int(inst.get('observations', 0))

            print(f"  {conf_bar} {int(conf*100):3d}%  {inst.get('id', 'unnamed')}")
            print(f"            trigger: {trigger}")
            print(f"            observations: {obs}")

            content = inst.get('content', '')
            action_match = re.search(r'## Action\s*\n\s*(.+?)(?:\n\n|\n##|$)', content, re.DOTALL)
            if action_match:
                action = action_match.group(1).strip().split('\n')[0]
                print(f"            action: {action[:60]}{'...' if len(action) > 60 else ''}")
            print()

    _print_data_stats()
    print(f"\n{'='*60}\n")
    return 0


def _print_data_stats():
    """Print data collection statistics."""
    jsonl = DATA_DIR / "turns.jsonl"
    if jsonl.exists():
        line_count = sum(1 for _ in open(jsonl))
        size_kb = jsonl.stat().st_size / 1024
        print(f"\n---")
        print(f"  Data: {line_count} events ({size_kb:.1f} KB)")
        print(f"  File: {jsonl}")

    index_file = DATA_DIR / "sessions" / "_index.json"
    if index_file.exists():
        try:
            d = json.loads(index_file.read_text())
            print(f"  Sessions: {d.get('total_sessions', 0)} sessions, "
                  f"{d.get('total_turns', 0)} turns")
        except Exception:
            pass

    registry_file = DATA_DIR / "task_registry.json"
    if registry_file.exists():
        try:
            d = json.loads(registry_file.read_text())
            tasks = d.get('tasks', {})
            dirty = d.get('dirty_task_ids', [])
            active = sum(1 for t in tasks.values() if t.get('status') == 'active')
            completed = sum(1 for t in tasks.values() if t.get('status') == 'completed')
            print(f"  Tasks: {len(tasks)} total ({active} active, {completed} completed)")
            if dirty:
                print(f"  Dirty: {len(dirty)} tasks pending analysis")
        except Exception:
            pass


# ─────────────────────────────────────────────
# Import Command
# ─────────────────────────────────────────────

def cmd_import(args):
    """Import instincts from file or URL."""
    source = args.source

    if source.startswith('http://') or source.startswith('https://'):
        print(f"Fetching from URL: {source}")
        try:
            with urllib.request.urlopen(source) as response:
                content = response.read().decode('utf-8')
        except Exception as e:
            print(f"Error fetching URL: {e}", file=sys.stderr)
            return 1
    else:
        path = Path(source).expanduser()
        if not path.exists():
            print(f"File not found: {path}", file=sys.stderr)
            return 1
        content = path.read_text()

    new_instincts = parse_instinct_file(content)
    if not new_instincts:
        print("No valid instincts found in source.")
        return 1

    print(f"\nFound {len(new_instincts)} instincts to import.\n")

    existing = load_all_instincts()
    existing_ids = {i.get('id') for i in existing}

    to_add = []
    duplicates = []
    to_update = []

    for inst in new_instincts:
        inst_id = inst.get('id')
        if inst_id in existing_ids:
            existing_inst = next((e for e in existing if e.get('id') == inst_id), None)
            if existing_inst and inst.get('confidence', 0) > existing_inst.get('confidence', 0):
                to_update.append(inst)
            else:
                duplicates.append(inst)
        else:
            to_add.append(inst)

    min_conf = args.min_confidence or 0.0
    to_add = [i for i in to_add if i.get('confidence', 0.5) >= min_conf]
    to_update = [i for i in to_update if i.get('confidence', 0.5) >= min_conf]

    if to_add:
        print(f"NEW ({len(to_add)}):")
        for inst in to_add:
            print(f"  + {inst.get('id')} (confidence: {inst.get('confidence', 0.5):.2f})")
    if to_update:
        print(f"\nUPDATE ({len(to_update)}):")
        for inst in to_update:
            print(f"  ~ {inst.get('id')} (confidence: {inst.get('confidence', 0.5):.2f})")
    if duplicates:
        print(f"\nSKIP ({len(duplicates)} - already exists with equal/higher confidence):")
        for inst in duplicates[:5]:
            print(f"  - {inst.get('id')}")
        if len(duplicates) > 5:
            print(f"  ... and {len(duplicates) - 5} more")

    if args.dry_run:
        print("\n[DRY RUN] No changes made.")
        return 0

    if not to_add and not to_update:
        print("\nNothing to import.")
        return 0

    if not args.force:
        response = input(f"\nImport {len(to_add)} new, update {len(to_update)}? [y/N] ")
        if response.lower() != 'y':
            print("Cancelled.")
            return 0

    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    source_name = Path(source).stem if not source.startswith('http') else 'web-import'
    output_file = INHERITED_DIR / f"{source_name}-{timestamp}.yaml"

    all_to_write = to_add + to_update
    output_content = f"# Imported from {source}\n# Date: {datetime.now().isoformat()}\n\n"

    for inst in all_to_write:
        output_content += "---\n"
        for key in ['id', 'type', 'trigger', 'confidence', 'domain',
                     'observations', 'first_seen', 'last_seen']:
            if inst.get(key) is not None:
                value = inst[key]
                if key == 'trigger':
                    output_content += f'{key}: "{value}"\n'
                else:
                    output_content += f"{key}: {value}\n"
        output_content += "source: inherited\n"
        output_content += f"imported_from: \"{source}\"\n"
        # Preserve nested blocks (e.g., intercept for bash_pattern)
        intercept = inst.get('intercept')
        if isinstance(intercept, dict) and intercept:
            output_content += "intercept:\n"
            for sub_key, sub_val in intercept.items():
                output_content += f'  {sub_key}: "{sub_val}"\n'
        output_content += "---\n\n"
        output_content += inst.get('content', '') + "\n\n"

    output_file.write_text(output_content)
    print(f"\nImport complete!")
    print(f"   Added: {len(to_add)}")
    print(f"   Updated: {len(to_update)}")
    print(f"   Saved to: {output_file}")
    return 0


# ─────────────────────────────────────────────
# Export Command
# ─────────────────────────────────────────────

def cmd_export(args):
    """Export instincts to file."""
    instincts = load_all_instincts()

    if not instincts:
        print("No instincts to export.")
        return 1

    if args.domain:
        instincts = [i for i in instincts
                     if i.get('domain') == args.domain or i.get('type') == args.domain]
    if args.min_confidence:
        instincts = [i for i in instincts if i.get('confidence', 0.5) >= args.min_confidence]

    if not instincts:
        print("No instincts match the criteria.")
        return 1

    output = f"# Instincts export\n# Date: {datetime.now().isoformat()}\n# Total: {len(instincts)}\n\n"
    for inst in instincts:
        output += "---\n"
        for key in ['id', 'type', 'trigger', 'confidence', 'domain',
                     'observations', 'first_seen', 'last_seen', 'source']:
            if inst.get(key) is not None:
                value = inst[key]
                if key == 'trigger':
                    output += f'{key}: "{value}"\n'
                else:
                    output += f"{key}: {value}\n"
        # Export nested blocks (e.g., intercept for bash_pattern)
        intercept = inst.get('intercept')
        if isinstance(intercept, dict) and intercept:
            output += "intercept:\n"
            for sub_key, sub_val in intercept.items():
                output += f'  {sub_key}: "{sub_val}"\n'
        output += "---\n\n"
        output += inst.get('content', '') + "\n\n"

    if args.output:
        Path(args.output).expanduser().write_text(output)
        print(f"Exported {len(instincts)} instincts to {args.output}")
    else:
        print(output)
    return 0


# ─────────────────────────────────────────────
# Evolve Command
# ─────────────────────────────────────────────

def cmd_evolve(args):
    """Analyze instincts and suggest evolutions."""
    instincts = load_all_instincts()

    if len(instincts) < 3:
        print("Need at least 3 instincts to analyze patterns.")
        print(f"Currently have: {len(instincts)}")
        return 1

    print(f"\n{'='*60}")
    print(f"  EVOLVE ANALYSIS - {len(instincts)} instincts")
    print(f"{'='*60}\n")

    # Group by type
    by_type = defaultdict(list)
    for inst in instincts:
        by_type[inst.get('type', inst.get('domain', 'general'))].append(inst)

    # High-confidence instincts (skill candidates)
    high_conf = [i for i in instincts if i.get('confidence', 0) >= 0.8]
    print(f"High confidence instincts (>=80%): {len(high_conf)}")

    # Find clusters by trigger similarity
    trigger_clusters = defaultdict(list)
    for inst in instincts:
        trigger = inst.get('trigger', '').lower()
        for word in ['when', 'creating', 'writing', 'adding', 'implementing',
                      'testing', 'a', 'the', 'for', 'in']:
            trigger = trigger.replace(word, '').strip()
        trigger_key = ' '.join(trigger.split())
        if trigger_key:
            trigger_clusters[trigger_key].append(inst)

    # Skill candidates (2+ instincts in cluster)
    skill_candidates = []
    for trigger, cluster in trigger_clusters.items():
        if len(cluster) >= 2:
            avg_conf = sum(i.get('confidence', 0.5) for i in cluster) / len(cluster)
            skill_candidates.append({
                'trigger': trigger,
                'instincts': cluster,
                'avg_confidence': avg_conf,
                'types': list(set(i.get('type', 'general') for i in cluster))
            })

    skill_candidates.sort(key=lambda x: (-len(x['instincts']), -x['avg_confidence']))

    if skill_candidates:
        print(f"\n## SKILL CANDIDATES ({len(skill_candidates)})\n")
        for i, cand in enumerate(skill_candidates[:5], 1):
            print(f"{i}. Cluster: \"{cand['trigger']}\"")
            print(f"   Instincts: {len(cand['instincts'])}")
            print(f"   Avg confidence: {cand['avg_confidence']:.0%}")
            print(f"   Types: {', '.join(cand['types'])}")
            for inst in cand['instincts'][:3]:
                print(f"     - {inst.get('id')}")
            print()

    # Command candidates (workflow with high confidence)
    workflow = [i for i in instincts
                if i.get('type') in ('strategy_selection', 'efficiency_hint')
                and i.get('confidence', 0) >= 0.7]
    if workflow:
        print(f"\n## COMMAND CANDIDATES ({len(workflow)})\n")
        for inst in workflow[:5]:
            trigger = inst.get('trigger', 'unknown')
            cmd_name = re.sub(r'[^a-z0-9-]', '-', trigger.lower())[:20].strip('-')
            print(f"  /{cmd_name}")
            print(f"    From: {inst.get('id')} ({inst.get('confidence', 0):.0%})")
            print()

    # Agent candidates (3+ instincts, high confidence)
    agent_candidates = [c for c in skill_candidates
                        if len(c['instincts']) >= 3 and c['avg_confidence'] >= 0.75]
    if agent_candidates:
        print(f"\n## AGENT CANDIDATES ({len(agent_candidates)})\n")
        for cand in agent_candidates[:3]:
            name = re.sub(r'[^a-z0-9-]', '-', cand['trigger'].lower())[:20].strip('-')
            print(f"  {name}-agent")
            print(f"    Covers {len(cand['instincts'])} instincts")
            print(f"    Avg confidence: {cand['avg_confidence']:.0%}")
            print()

    if args.generate:
        print("\n[Would generate evolved structures]")
        print(f"  Skills:   {EVOLVED_DIR / 'skills'}")
        print(f"  Commands: {EVOLVED_DIR / 'commands'}")
        print(f"  Agents:   {EVOLVED_DIR / 'agents'}")

    print(f"\n{'='*60}\n")
    return 0


# ─────────────────────────────────────────────
# Observer Command
# ─────────────────────────────────────────────

def cmd_observer(args):
    """Manage observer daemon."""
    action = args.action

    if not DAEMON_SCRIPT.exists():
        print(f"Daemon script not found: {DAEMON_SCRIPT}", file=sys.stderr)
        return 1

    result = subprocess.run(
        [str(DAEMON_SCRIPT), action],
        capture_output=False
    )
    return result.returncode


# ─────────────────────────────────────────────
# Materialize Command
# ─────────────────────────────────────────────

def cmd_materialize(args):
    """Generate rules and prompt templates from high-confidence instincts."""
    config = load_config()
    threshold = config.get('materialization', {}).get('rule_threshold', 0.7)
    max_rules = 20

    instincts = load_all_instincts()
    high_conf = [i for i in instincts if i.get('confidence', 0) >= threshold]

    if not high_conf:
        print(f"No instincts above threshold ({threshold})")
        return 0

    print(f"Materializing {len(high_conf)} high-confidence instincts...\n")

    # Sort by confidence descending, take top max_rules
    high_conf.sort(key=lambda x: -x.get('confidence', 0))
    rules = high_conf[:max_rules]

    # Generate rules file content
    header = (
        "# Learned Preferences (auto-generated by continual-learning-v3)\n"
        f"# Last updated: {datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        "# Source: observer v3 pattern detection\n"
        "# Do not edit manually - changes will be overwritten\n\n"
    )

    # Group by domain/type
    by_category = defaultdict(list)
    for inst in rules:
        cat = inst.get('domain', inst.get('type', 'general'))
        by_category[cat].append(inst)

    content = header
    for cat in sorted(by_category.keys()):
        cat_label = cat.replace('_', ' ').title()
        content += f"## {cat_label}\n"
        for inst in by_category[cat]:
            # Extract action from content
            action = ""
            inst_content = inst.get('content', '')
            action_match = re.search(r'## Action\s*\n(.+?)(?:\n\n|\n##|$)',
                                      inst_content, re.DOTALL)
            if action_match:
                action = action_match.group(1).strip().split('\n')[0]
            else:
                action = inst.get('trigger', inst.get('id', 'unknown'))

            conf = inst.get('confidence', 0)
            content += f"- {action} (confidence: {conf:.2f})\n"
        content += "\n"

    # Build delegation prompt templates
    prompt_templates: dict[str, str] = {}
    delegation = [i for i in high_conf if i.get('type') == 'delegation_preference']
    if delegation:
        by_agent = defaultdict(list)
        for inst in delegation:
            content_text = inst.get('content', '')
            agent = 'general'
            for a in ['Explore', 'Bash', 'Plan', 'code-reviewer',
                       'security-reviewer', 'tdd-guide']:
                if a.lower() in inst.get('id', '').lower() or a.lower() in content_text.lower():
                    agent = a
                    break
            by_agent[agent].append(inst)

        for agent_type, agent_insts in by_agent.items():
            prompt_content = f"# Learned constraints for {agent_type} agent delegation\n\n"
            for inst in agent_insts:
                action_match = re.search(r'## Action\s*\n(.+?)(?:\n\n|\n##|$)',
                                          inst.get('content', ''), re.DOTALL)
                if action_match:
                    prompt_content += f"- {action_match.group(1).strip()}\n"
            prompt_templates[agent_type] = prompt_content

    if args.dry_run:
        print(f"[DRY RUN] Would write rules to: {RULES_FILE}")
        print(f"  {len(rules)} rules would be materialized")
        for agent_type in prompt_templates:
            print(f"  Would write prompt template: {PROMPTS_DIR / f'{agent_type}.md'}")
        bash_instincts = _load_bash_instincts()
        if bash_instincts:
            print(f"  Would sync bash insights: {BASH_INSIGHTS_RULE} ({len(bash_instincts)} insights)")
        print("\n[DRY RUN] No changes made.")
    else:
        # Write rules file
        RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        RULES_FILE.write_text(content)
        print(f"Rules written to: {RULES_FILE}")
        print(f"  {len(rules)} rules materialized")

        # Write delegation prompt templates
        for agent_type, prompt_content in prompt_templates.items():
            prompt_file = PROMPTS_DIR / f"{agent_type}.md"
            prompt_file.write_text(prompt_content)
            print(f"  Prompt template: {prompt_file}")

        # Sync bash_insights.md alongside materialization
        bash_instincts = _load_bash_instincts()
        if bash_instincts:
            _sync_bash_insights_rule(bash_instincts)
            print(f"  Bash insights: {BASH_INSIGHTS_RULE} ({len(bash_instincts)} insights)")

        print("\nMaterialization complete.")

    return 0


# ─────────────────────────────────────────────
# Bash Insight Command
# ─────────────────────────────────────────────

BASH_INSIGHTS_RULE = Path.home() / ".claude" / "rules" / "bash_insights.md"


def _load_bash_instincts() -> list[dict]:
    """Load all bash_pattern instincts."""
    instincts = []
    for filepath in sorted(PERSONAL_DIR.glob("*.yaml")):
        try:
            content = filepath.read_text()
            parsed = parse_instinct_file(content)
            for inst in parsed:
                if inst.get('type') == 'bash_pattern':
                    inst['_source_file'] = str(filepath)
                    inst['_raw_content'] = content
                    instincts.append(inst)
        except Exception:
            continue
    return instincts


def _sync_bash_insights_rule(instincts: list[dict]):
    """Regenerate ~/.claude/rules/bash_insights.md from current bash instincts."""
    config = load_config()
    block_threshold = config.get('bash_intercept', {}).get('block_threshold', 0.7)

    active = [i for i in instincts if i.get('confidence', 0) >= 0.3]
    active.sort(key=lambda x: -x.get('confidence', 0))

    header = (
        "# Bash Tool Insights (auto-generated by continual-learning-v3)\n"
        f"# Last updated: {datetime.now().strftime('%Y-%m-%d')}\n"
        "# Source: observer v3 bash pattern detection\n"
        "# Do not edit manually - regenerate with: cl bash-insight sync\n\n"
    )

    if not active:
        content = header + (
            "There are 0 active bash command insights. These represent learned preferences\n"
            "for how certain bash commands should be executed.\n\n"
            "## Active Insights Summary\n\n"
            "No bash insights detected yet. Insights will be added automatically by the\n"
            "observer when recurring bash command corrections are detected, or manually\n"
            "via `cl bash-insight add`.\n"
        )
    else:
        content = header + (
            f"There are {len(active)} active bash command insights. These represent learned "
            "preferences\nfor how certain bash commands should be executed.\n\n"
            "## Active Insights Summary\n\n"
            "| Domain | Insight | Confidence | Blocking |\n"
            "|--------|---------|------------|----------|\n"
        )
        for inst in active:
            domain = inst.get('domain', 'general')
            trigger = inst.get('trigger', 'unknown')
            conf = inst.get('confidence', 0)
            blocking = "Yes" if conf >= block_threshold else "Warn only"
            content += f"| {domain} | {trigger} | {conf:.2f} | {blocking} |\n"

    content += (
        "\n## How to Use\n\n"
        "- Before running bash commands in flagged domains, consider the alternative\n"
        "- If a bash command is blocked by an insight, the block message will explain why\n"
        "- To intentionally bypass: prepend `CL_SKIP=<insight_id>` to the command\n"
        "- To disable all interception for one command: prepend `CL_BASH_INTERCEPT=0`\n"
        "- To view full details: use `cl bash-insight list`\n"
    )

    BASH_INSIGHTS_RULE.parent.mkdir(parents=True, exist_ok=True)
    BASH_INSIGHTS_RULE.write_text(content)


def cmd_bash_insight(args):
    """Manage bash command insights."""
    action = args.bash_action

    if action == 'list':
        instincts = _load_bash_instincts()
        if not instincts:
            print("No bash insights found.")
            print(f"\nAdd one with: cl bash-insight add --regex '...' --action '...'")
            return 0

        config = load_config()
        block_threshold = config.get('bash_intercept', {}).get('block_threshold', 0.7)

        print(f"\n{'='*60}")
        print(f"  BASH INSIGHTS - {len(instincts)} total")
        print(f"{'='*60}\n")

        for inst in sorted(instincts, key=lambda x: -x.get('confidence', 0)):
            conf = inst.get('confidence', 0)
            conf_bar = '\u2588' * int(conf * 10) + '\u2591' * (10 - int(conf * 10))
            status = "BLOCKING" if conf >= block_threshold else "warning"
            inst_id = inst.get('id', 'unknown')

            print(f"  {conf_bar} {int(conf*100):3d}%  [{status}]  {inst_id}")
            print(f"            trigger: {inst.get('trigger', 'unknown')}")

            # Show intercept info from raw content
            raw = inst.get('_raw_content', '')
            regex_match = re.search(r'regex:\s*["\']?(.+?)["\']?\s*$', raw, re.MULTILINE)
            bypass_match = re.search(r'bypass_env:\s*["\']?(.+?)["\']?\s*$', raw, re.MULTILINE)
            if regex_match:
                print(f"            regex: {regex_match.group(1)}")
            if bypass_match:
                print(f"            bypass: {bypass_match.group(1)}=1")

            content = inst.get('content', '')
            action_match = re.search(r'## Action\s*\n\s*(.+?)(?:\n\n|\n##|$)', content, re.DOTALL)
            if action_match:
                action_text = action_match.group(1).strip().split('\n')[0]
                print(f"            action: {action_text[:60]}{'...' if len(action_text) > 60 else ''}")
            print()

        print(f"{'='*60}\n")
        return 0

    elif action == 'add':
        regex = args.regex
        action_text = args.action_text
        confidence = args.confidence or 0.5
        bypass_env = args.bypass_env or ''
        trigger = args.trigger or f"when running commands matching /{regex}/"

        if not regex:
            print("Error: --regex is required", file=sys.stderr)
            return 1
        if not action_text:
            print("Error: --action is required", file=sys.stderr)
            return 1

        # Validate regex
        try:
            re.compile(regex)
        except re.error as e:
            print(f"Error: invalid regex: {e}", file=sys.stderr)
            return 1

        # Generate ID from regex
        inst_id = re.sub(r'[^a-z0-9]', '_', regex.lower())[:30].strip('_')
        inst_id = f"bash_{inst_id}"

        today = datetime.now().strftime('%Y-%m-%d')

        filename = f"bash_pattern_{inst_id}.yaml"
        filepath = PERSONAL_DIR / filename

        intercept_block = f'  regex: "{regex}"'
        if bypass_env:
            intercept_block += f'\n  bypass_env: "{bypass_env}"'

        content = (
            f"---\n"
            f"id: {inst_id}\n"
            f"type: bash_pattern\n"
            f'trigger: "{trigger}"\n'
            f"confidence: {confidence}\n"
            f"domain: tool_use\n"
            f"observations: 1\n"
            f'first_seen: "{today}"\n'
            f'last_seen: "{today}"\n'
            f"source: manual\n"
            f"intercept:\n"
            f"{intercept_block}\n"
            f"---\n\n"
            f"## Pattern\n"
            f"Manually added bash command insight.\n\n"
            f"## Action\n"
            f"{action_text}\n\n"
            f"## Evidence\n"
            f"- Manually added on {today}\n"
        )

        filepath.write_text(content)
        print(f"Created bash insight: {inst_id}")
        print(f"  File: {filepath}")
        print(f"  Confidence: {confidence}")
        print(f"  Regex: {regex}")

        # Sync rules file
        all_instincts = _load_bash_instincts()
        _sync_bash_insights_rule(all_instincts)
        print(f"  Updated: {BASH_INSIGHTS_RULE}")

        return 0

    elif action == 'test':
        command = args.argument
        if not command:
            print("Error: provide a command to test", file=sys.stderr)
            return 1

        instincts = _load_bash_instincts()
        if not instincts:
            print("No bash insights to test against.")
            return 0

        config = load_config()
        block_threshold = config.get('bash_intercept', {}).get('block_threshold', 0.7)

        print(f"\nTesting command: {command}\n")
        matched = False

        for inst in instincts:
            raw = inst.get('_raw_content', '')
            regex_match = re.search(r'regex:\s*["\']?(.+?)["\']?\s*$', raw, re.MULTILINE)
            if not regex_match:
                continue

            regex_str = regex_match.group(1)
            try:
                if re.search(regex_str, command):
                    matched = True
                    conf = inst.get('confidence', 0)
                    status = "WOULD BLOCK" if conf >= block_threshold else "WOULD WARN"
                    print(f"  MATCH [{status}]: {inst.get('id', 'unknown')}")
                    print(f"    regex: {regex_str}")
                    print(f"    confidence: {conf:.2f}")

                    content = inst.get('content', '')
                    action_match_re = re.search(r'## Action\s*\n(.+?)(?:\n##|\Z)', content, re.DOTALL)
                    if action_match_re:
                        print(f"    action: {action_match_re.group(1).strip()}")
                    print()
            except re.error:
                continue

        if not matched:
            print("  No insights match this command. It would pass through.")

        return 0

    elif action == 'disable':
        inst_id = args.argument
        if not inst_id:
            print("Error: provide an insight ID to disable", file=sys.stderr)
            return 1

        instincts = _load_bash_instincts()
        found = None
        for inst in instincts:
            if inst.get('id') == inst_id:
                found = inst
                break

        if not found:
            print(f"Insight not found: {inst_id}", file=sys.stderr)
            return 1

        # Set confidence to 0 to effectively disable
        filepath = Path(found['_source_file'])
        content = filepath.read_text()
        content = re.sub(
            r'^confidence:\s*[\d.]+',
            'confidence: 0.0',
            content,
            flags=re.MULTILINE
        )
        filepath.write_text(content)
        print(f"Disabled insight: {inst_id} (confidence set to 0.0)")

        # Sync rules file
        all_instincts = _load_bash_instincts()
        _sync_bash_insights_rule(all_instincts)
        print(f"Updated: {BASH_INSIGHTS_RULE}")
        return 0

    elif action == 'sync':
        instincts = _load_bash_instincts()
        _sync_bash_insights_rule(instincts)
        print(f"Synced {len(instincts)} bash insights to: {BASH_INSIGHTS_RULE}")
        return 0

    else:
        print(f"Unknown action: {action}")
        return 1


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Instinct CLI for Continuous Learning v3',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s status                    Show all instincts
  %(prog)s observer start            Start observer daemon
  %(prog)s observer status           Check observer status
  %(prog)s export -o backup.yaml     Export instincts
  %(prog)s import teammate.yaml      Import instincts
  %(prog)s materialize               Generate rules from instincts
  %(prog)s evolve --generate         Suggest and generate skills
  %(prog)s bash-insight list         List bash insights
  %(prog)s bash-insight add --regex "..." --action "..."  Add insight
  %(prog)s bash-insight test "curl https://api.github.com/..."  Test command
  %(prog)s bash-insight disable <id> Disable an insight
  %(prog)s bash-insight sync         Regenerate bash_insights.md
""")
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # Status
    subparsers.add_parser('status', help='Show instinct status')

    # Import
    import_parser = subparsers.add_parser('import', help='Import instincts')
    import_parser.add_argument('source', help='File path or URL')
    import_parser.add_argument('--dry-run', action='store_true', help='Preview only')
    import_parser.add_argument('--force', action='store_true', help='Skip confirmation')
    import_parser.add_argument('--min-confidence', type=float, help='Min confidence threshold')

    # Export
    export_parser = subparsers.add_parser('export', help='Export instincts')
    export_parser.add_argument('--output', '-o', help='Output file')
    export_parser.add_argument('--domain', help='Filter by domain/type')
    export_parser.add_argument('--min-confidence', type=float, help='Min confidence')

    # Evolve
    evolve_parser = subparsers.add_parser('evolve', help='Analyze and evolve instincts')
    evolve_parser.add_argument('--generate', action='store_true', help='Generate structures')

    # Observer
    observer_parser = subparsers.add_parser('observer', help='Manage observer daemon')
    observer_parser.add_argument('action', choices=['start', 'stop', 'status'],
                                  help='Daemon action')

    # Materialize
    mat_parser = subparsers.add_parser('materialize', help='Generate rules/prompts')
    mat_parser.add_argument('--dry-run', action='store_true', help='Preview only')

    # Bash Insight
    bash_parser = subparsers.add_parser('bash-insight', help='Manage bash command insights')
    bash_parser.add_argument('bash_action',
                              choices=['list', 'add', 'test', 'disable', 'sync'],
                              help='Bash insight action')
    bash_parser.add_argument('argument', nargs='?',
                              help='Command to test (for test) or insight ID (for disable)')
    bash_parser.add_argument('--regex', help='Regex pattern to match (for add)')
    bash_parser.add_argument('--action', dest='action_text', help='Action text (for add)')
    bash_parser.add_argument('--confidence', type=float, help='Initial confidence (for add)')
    bash_parser.add_argument('--bypass-env', help='Bypass env var name (for add)')
    bash_parser.add_argument('--trigger', help='Trigger description (for add)')

    args = parser.parse_args()

    commands = {
        'status': cmd_status,
        'import': cmd_import,
        'export': cmd_export,
        'evolve': cmd_evolve,
        'observer': cmd_observer,
        'materialize': cmd_materialize,
        'bash-insight': cmd_bash_insight,
    }

    handler = commands.get(args.command)
    if handler:
        return handler(args)
    else:
        parser.print_help()
        return 1


if __name__ == '__main__':
    sys.exit(main() or 0)
