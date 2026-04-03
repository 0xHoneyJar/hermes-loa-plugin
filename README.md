# hermes-loa-plugin

Hermes plugin for the [Loa](https://github.com/0xHoneyJar/loa) construct network. Bridges construct personas, composition, and mode switching into the Hermes agent runtime.

Hermes is the harness. Loa constructs are the horse.

## What This Does

When you say "FEEL mode" or "load kansei", this plugin:

1. **Indexes** all construct packs from `~/.loa/constructs/packs/` (L1)
2. **Resolves** the construct by name, mode, or vocabulary match (L2)
3. **Loads** the persona identity and injects it into the LLM context
4. **Composes** related constructs via `compose_with` relationships (L3)
5. **Tracks** mode transitions across turns within a session

No construct content is hardcoded. Everything is discovered dynamically from your installed packs.

## What This Does NOT Do

- **Prescribe modes.** You define your own modes by composing constructs. The plugin ships with zero default modes. Example archetypes are in `examples/`.
- **Replace Loa.** Loa runs inside Claude Code with slash commands, golden paths, and workflow gates. This plugin runs inside Hermes — a different runtime with different strengths (cross-session memory, web research, delegation, scheduling, multi-platform).
- **Read your code.** The plugin only reads `construct.yaml` and `identity/` files. It never touches your source code, grimoires, or application state.

## Architecture

Based on [RFC #452: First-Class Construct Support in Loa](https://github.com/0xHoneyJar/loa/issues/452).

```
┌─────────────────────────────────────────────────────────┐
│  Hermes Agent Runtime                                   │
│                                                         │
│  ┌─────────────┐    ┌──────────────────────────────┐    │
│  │ System      │    │ Loa Plugin                   │    │
│  │ Prompt      │◄───│                              │    │
│  │             │    │  L1: ConstructIndex           │    │
│  │ + injected  │    │  L2: Name Resolution         │    │
│  │   persona   │    │  L3: Composition Routing     │    │
│  │   context   │    │  L4: User-Defined Modes      │    │
│  └─────────────┘    │  L5: Session Greeting        │    │
│                     └──────────┬───────────────────┘    │
│                                │                        │
└────────────────────────────────┼────────────────────────┘
                                 │ reads
                                 ▼
                  ~/.loa/constructs/packs/
                  ├── artisan/
                  │   ├── construct.yaml
                  │   ├── identity/
                  │   │   ├── persona.yaml
                  │   │   ├── expertise.yaml
                  │   │   └── ALEXANDER.md
                  │   └── skills/
                  ├── k-hole/
                  ├── observer/
                  └── ...
```

### Layers (from RFC #452)

| Layer | What | Plugin Implementation |
|-------|------|----------------------|
| **L1** | Construct Index | `ConstructIndex.build()` — scans packs, builds domain + keyword indices |
| **L2** | Name Resolution | `_detect_activation()` — explicit mode, direct reference, implicit vocab |
| **L3** | Composition as Pipe | `compose_with` from `construct.yaml` → secondary context injection |
| **L4** | Personal Operator OS | `ArchetypeConfig` loaded from `loa-plugin.yaml` — your modes, your rules |
| **L5** | Ambient Presence | Session greeting showing indexed packs and available modes |

### Hermes Hook Contract

| Hook | When | What the Plugin Does |
|------|------|---------------------|
| `on_session_start` | Session begins | Build construct index, load archetype config |
| `pre_llm_call` | Before every LLM call | Detect activation, inject persona (≤1500 chars) |
| `post_llm_call` | After every LLM response | Observe mode transition signals |
| `on_session_end` | Session ends | Cleanup, log session path |

The `pre_llm_call` return value `{"context": "..."}` is appended to the ephemeral system prompt for that single turn. It is NOT persisted to session history or cache.

## Install

### Prerequisites

- [Hermes](https://github.com/hermes-ai/hermes-agent) installed and running
- [Loa constructs](https://github.com/0xHoneyJar/loa-constructs) installed at `~/.loa/constructs/packs/`
- Python `pyyaml` available in the Hermes venv

### Setup

```bash
# Clone into the Hermes plugins directory
git clone https://github.com/0xHoneyJar/hermes-loa-plugin ~/.hermes/plugins/loa

# Verify
hermes plugins list
# Should show: loa | enabled | 1.1.0
```

### Configure Your Modes (Optional)

Copy an example archetype or write your own:

```bash
# Use the Operator OS v2 archetype
cp ~/.hermes/plugins/loa/examples/operator-os-v2.yaml ~/.loa/loa-plugin.yaml

# Or the minimal dagger archetype (no modes, direct activation only)
cp ~/.hermes/plugins/loa/examples/dagger-focus.yaml ~/.loa/loa-plugin.yaml

# Or write your own — see examples/ for the format
```

If no `loa-plugin.yaml` is found, the plugin still works — you just activate constructs directly ("load kansei", "use artisan") instead of through named modes.

## Usage

### Direct Construct Activation (Always Works)

```
> load kansei
  → Loads kansei persona, activates compose_with partners

> use artisan
  → Loads ALEXANDER persona from artisan/identity/

> activate observer
  → Loads KEEPER persona from observer/identity/
```

### Mode Activation (Requires loa-plugin.yaml)

```
> FEEL mode
  → Activates artisan + ALEXANDER (if you defined a FEEL mode)

> DIG mode
  → Activates k-hole + STAMETS (if you defined a DIG mode)
```

### Implicit Activation (From Vocabulary)

If your archetype defines vocab triggers, the plugin detects domain language:

```
> I want to research the plugin landscape and investigate alternatives
  → 2+ DIG vocab matches → activates DIG mode automatically
```

This requires 2+ vocabulary matches to avoid false positives.

### Session Greeting (First Turn)

On the first turn of each session, the plugin injects a brief status:

```
[Loa: 28 constructs indexed]
[Modes: ARCH, DIG, FEEL, FRAME, SHIP, TEND]
[Active: observer, artisan, k-hole]
```

## Configuration Reference

### loa-plugin.yaml

```yaml
# Define your modes by composing constructs
modes:
  <mode_name>:
    constructs: [<primary_pack>, <secondary_pack>, ...]  # first = primary
    persona: <PERSONA_NAME>                                # display name
    vocab: [<word1>, <word2>, ...]                         # implicit triggers
    gates: {}                                              # optional workflow gates

# Which constructs are always loaded
active_constructs: [<pack1>, <pack2>]

# Default entry command
entry_point: /observe
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LOA_HOME` | `~/.loa` | Root directory for Loa installation |

### Config File Search Order

1. `~/.loa/loa-plugin.yaml`
2. `~/.hermes/plugins/loa/loa-plugin.yaml`
3. No config → direct activation only (no modes)

## Extension Points

### Custom Context Injection

Override `_load_extensions()` in `__init__.py` to inject additional context per turn:

```python
def _load_extensions(state, user_message):
    """Your custom context injection logic."""
    if "deploy" in user_message.lower():
        return "[Deploy target: production-us-east-1]"
    return None
```

See `examples/hivemind_extension.py` for a complete organizational memory pattern.

### Adding Construct Packs

```bash
cd ~/.loa/constructs/packs
gh repo clone 0xHoneyJar/construct-<name> <name> -- --depth 1

# Verify
test -f <name>/construct.yaml && echo "valid pack"

# Plugin auto-indexes on next session — no restart needed
```

## Construct Pack Contract

For a pack to be indexable by this plugin, it needs:

```
<pack>/
  construct.yaml          # REQUIRED — pack metadata
  identity/               # OPTIONAL — persona files
    persona.yaml          #   cognitive frame, voice, markers
    expertise.yaml        #   domain specializations
    *.md                  #   identity narrative
  skills/                 # OPTIONAL — Hermes-discoverable skills
    <skill>/SKILL.md      #   standard Hermes skill format
```

### construct.yaml Minimum

```yaml
name: my-construct
description: "What this construct does"
short_description: "One line"
domain: [design, engineering]
skills:
  - slug: my-skill
compose_with:
  - artisan
  - observer
```

## Pitfalls

- **Persona YAML dump trap**: `expertise.yaml` contains nested dicts. The plugin extracts scalar fields only (name, depth) — never dumps raw YAML into the prompt.
- **Mixed type fields**: `skills` and `compose_with` in `construct.yaml` can be dicts or strings depending on the pack. The plugin handles both.
- **Mode detection false positives**: Common words like "research" or "deploy" appear in normal conversation. Implicit detection requires 2+ vocabulary matches. Explicit "X mode" is always authoritative.
- **Plugin state is per-process**: Session state lives in memory. If Hermes restarts, mode state resets. Users re-invoke naturally.
- **Hermes caches the skills index**: After changing SKILL.md files, the cache may be stale. Restart Hermes or wait for mtime invalidation.
- **1500 char persona cap**: Identity injection is budgeted. If your persona.yaml + expertise.yaml + identity.md exceed 1500 chars, the result is truncated.

## Design Philosophy

From [RFC #452](https://github.com/0xHoneyJar/loa/issues/452):

> "Loa should support how people actually work — not prescribe how they should work."

> "The framework's job is not to build tools for other people. It's to build structure that helps people do deeper work for themselves and better learn how they work."

```
present options → honor the choice → step aside → be there when called
```

This plugin enables construct-aware AI sessions. It does not enforce workflows, gate progress, or prescribe how you should think. You compose constructs into the modes that match how YOU work.

## License

AGPL-3.0 — same as [Loa](https://github.com/0xHoneyJar/loa).
