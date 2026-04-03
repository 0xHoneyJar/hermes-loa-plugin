"""
Loa Constructs Plugin for Hermes Agent
=======================================

Bridges the Loa construct network into Hermes runtime.
The horse speaks through the harness.

RFC #452 Architecture (L1–L4):
  L1: Construct Index — dynamic discovery from ~/.loa/constructs/packs/
  L2: Name Resolution — lookup, load persona, activate skills
  L3: Composition as Pipe — compose_with relationships route context
  L4: Personal Operator OS — user-defined modes, not prescribed

Design principle from the RFC:
  "present options → honor the choice → step aside → be there when called"

Modes are a methodology, not a feature. This plugin ships with NO
default modes. Users define their own by composing constructs in
a config file (loa-plugin.yaml). Example archetypes are provided
in examples/ but never force-loaded.

Integration surface:
  on_session_start  → build construct index, load user archetype
  pre_llm_call      → detect activation, inject persona context
  post_llm_call     → observe mode transition signals
  on_session_end    → cleanup session state

Extension points:
  - loa-plugin.yaml: user-defined modes, vocab, composition rules
  - _load_extensions(): custom context injection hook
  - LOA_HOME env var: override default ~/.loa location
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("hermes.plugins.loa")

# ---------------------------------------------------------------------------
# Constants & Paths
# ---------------------------------------------------------------------------

LOA_HOME = Path(os.environ.get("LOA_HOME", os.path.expanduser("~/.loa")))
CONSTRUCTS_DIR = LOA_HOME / "constructs" / "packs"
PLUGIN_DIR = Path(__file__).parent

# Max chars for injected persona context
MAX_PERSONA_CHARS = 1500

# Direct construct activation regex: "load kansei", "use rosenzu", etc.
DIRECT_CONSTRUCT = re.compile(
    r"\b(?:load|use|activate|switch\s+to)\s+(\w[\w-]*)", re.I
)


# ---------------------------------------------------------------------------
# User-Defined Archetype Config
# ---------------------------------------------------------------------------

class ArchetypeConfig:
    """User-defined modes loaded from loa-plugin.yaml.

    The plugin ships with NO default modes. Users define their own
    by composing constructs. This respects the RFC #452 principle:
    "Operator OS is a methodology, not a feature."

    Example loa-plugin.yaml:

        modes:
          dig:
            constructs: [k-hole]
            persona: STAMETS
            vocab: [research, explore, deep dive, investigate]
          feel:
            constructs: [artisan, observer]
            persona: ALEXANDER
            vocab: [pixel, motion, weight, material, texture]

        active_constructs: [observer, artisan, k-hole]
        entry_point: /observe
    """

    def __init__(self):
        self.modes: Dict[str, Dict[str, Any]] = {}
        self.mode_triggers: Dict[str, re.Pattern] = {}
        self.active_constructs: List[str] = []
        self.entry_point: str = ""
        self.raw: Dict[str, Any] = {}

    def load(self, config_path: Optional[Path] = None):
        """Load archetype config from yaml file.

        Search order:
          1. Explicit path
          2. ~/.loa/loa-plugin.yaml
          3. ~/.hermes/plugins/loa/loa-plugin.yaml
          4. No config → no modes (just direct construct activation)
        """
        paths = []
        if config_path:
            paths.append(config_path)
        paths.extend([
            LOA_HOME / "loa-plugin.yaml",
            PLUGIN_DIR / "loa-plugin.yaml",
        ])

        for p in paths:
            if p.exists():
                try:
                    import yaml
                    self.raw = yaml.safe_load(p.read_text()) or {}
                    logger.info("Loa: loaded archetype config from %s", p)
                    break
                except Exception as e:
                    logger.warning("Loa: failed to parse %s: %s", p, e)
                    continue

        if not self.raw:
            logger.info("Loa: no archetype config found — direct construct activation only")
            return

        # Parse modes
        modes_raw = self.raw.get("modes", {})
        for mode_name, mode_def in modes_raw.items():
            if not isinstance(mode_def, dict):
                continue

            self.modes[mode_name.upper()] = {
                "constructs": mode_def.get("constructs", []),
                "persona": mode_def.get("persona", ""),
                "gates": mode_def.get("gates", {}),
            }

            # Build vocabulary trigger from user-defined vocab list
            vocab = mode_def.get("vocab", [])
            if vocab:
                # Escape each vocab entry for regex, join with |
                escaped = [re.escape(v) for v in vocab]
                pattern = r"\b(?:" + "|".join(escaped) + r")\b"
                self.mode_triggers[mode_name.upper()] = re.compile(pattern, re.I)

        self.active_constructs = self.raw.get("active_constructs", [])
        self.entry_point = self.raw.get("entry_point", "")

        logger.info(
            "Loa: archetype loaded — %d modes: %s",
            len(self.modes), ", ".join(self.modes.keys()),
        )

    def build_explicit_mode_pattern(self) -> Optional[re.Pattern]:
        """Build regex for explicit mode triggers from configured modes.

        Matches patterns like "DIG mode", "FEEL mode", etc.
        Only builds from modes the user has actually defined.
        """
        if not self.modes:
            return None
        mode_names = "|".join(re.escape(m) for m in self.modes.keys())
        return re.compile(
            rf"\b({mode_names})\s+mode\b", re.IGNORECASE
        )


# ---------------------------------------------------------------------------
# Construct Index — L1 from RFC #452
# ---------------------------------------------------------------------------

class ConstructEntry:
    """Indexed metadata for a single construct pack."""

    __slots__ = (
        "slug", "name", "description", "short_description",
        "domain", "compose_with", "skills", "has_identity",
        "persona_name", "events",
    )

    def __init__(self, slug: str, data: Dict[str, Any], pack_dir: Path):
        self.slug = slug
        self.name = data.get("name", slug)
        self.description = data.get("description", "")
        self.short_description = data.get("short_description", "")
        self.domain = data.get("domain", [])
        self.compose_with = data.get("compose_with", [])
        raw_skills = data.get("skills", [])
        # Handle mixed types: skills can be dicts or strings
        self.skills = [
            (s.get("slug", "") if isinstance(s, dict) else str(s))
            for s in raw_skills
        ]
        self.has_identity = (pack_dir / "identity").is_dir()
        self.events = data.get("events", {})

        # Detect primary persona name from identity files
        self.persona_name = ""
        if self.has_identity:
            identity_dir = pack_dir / "identity"
            persona_file = identity_dir / "persona.yaml"
            if persona_file.exists():
                try:
                    import yaml
                    pdata = yaml.safe_load(persona_file.read_text()) or {}
                    self.persona_name = pdata.get("name", "")
                except Exception:
                    pass
            if not self.persona_name:
                for md in sorted(identity_dir.glob("*.md")):
                    if not md.name.startswith("."):
                        self.persona_name = md.stem
                        break


class ConstructIndex:
    """Full index of all construct packs, built from filesystem.

    This is L1 from RFC #452 — the keystone layer.
    Enables: name resolution, composition routing, ambient awareness.
    """

    def __init__(self):
        self.packs: Dict[str, ConstructEntry] = {}
        self.domain_index: Dict[str, List[str]] = {}
        self.keyword_index: Dict[str, str] = {}

    def build(self):
        """Scan all packs and build indices."""
        if not CONSTRUCTS_DIR.exists():
            logger.warning("Constructs dir not found: %s", CONSTRUCTS_DIR)
            return

        for pack_dir in sorted(CONSTRUCTS_DIR.iterdir()):
            if not pack_dir.is_dir():
                continue

            slug = pack_dir.name
            data = _load_construct_yaml(slug)
            if not data:
                continue

            entry = ConstructEntry(slug, data, pack_dir)
            self.packs[slug] = entry

            for d in entry.domain:
                self.domain_index.setdefault(d, []).append(slug)

            stop_words = {
                "that", "this", "with", "from", "into", "your",
                "what", "when", "where", "does", "have", "been",
                "will", "they", "them", "than", "more", "also",
            }
            words = set()
            for text in [entry.name, entry.description, entry.short_description]:
                words.update(
                    w.lower() for w in re.findall(r'\b\w{4,}\b', text)
                    if w.lower() not in stop_words
                )
            for w in words:
                if w not in self.keyword_index:
                    self.keyword_index[w] = slug

        logger.info(
            "Loa index: %d packs, %d domains, %d keywords",
            len(self.packs), len(self.domain_index), len(self.keyword_index),
        )

    def find_by_domain(self, domain: str) -> List[str]:
        return self.domain_index.get(domain, [])

    def find_by_keyword(self, text: str) -> Optional[str]:
        """Find a construct pack by keyword match in text (3+ required)."""
        text_lower = text.lower()
        pack_scores: Dict[str, int] = {}

        for word, slug in self.keyword_index.items():
            if word in text_lower:
                pack_scores[slug] = pack_scores.get(slug, 0) + 1

        if pack_scores:
            best_slug = max(pack_scores, key=pack_scores.get)
            if pack_scores[best_slug] >= 3:
                return best_slug
        return None

    def get_compose_partners(self, slug: str) -> List[str]:
        """Get packs that compose with the given pack (L3: Composition)."""
        entry = self.packs.get(slug)
        if not entry:
            return []
        partners = []
        for c in entry.compose_with:
            if isinstance(c, dict):
                s = c.get("slug", "")
            elif isinstance(c, str):
                s = c
            else:
                continue
            if s and s in self.packs:
                partners.append(s)
        return partners


# ---------------------------------------------------------------------------
# Session State
# ---------------------------------------------------------------------------

class LoaSessionState:
    """Per-session state tracking active constructs and mode."""

    def __init__(self):
        self.active_mode: Optional[str] = None
        self.active_pack: Optional[str] = None
        self.active_persona: Optional[str] = None
        self.secondary_packs: List[str] = []
        self.persona_context: str = ""
        self.mode_history: List[str] = []
        self.turn_count: int = 0
        self.index: Optional[ConstructIndex] = None
        self.archetype: Optional[ArchetypeConfig] = None

    def set_mode(self, mode: str) -> bool:
        """Set active mode from user-defined archetype. Returns True if changed."""
        if mode == self.active_mode:
            return False

        prev = self.active_mode
        self.active_mode = mode

        # Resolve pack from archetype config
        if self.archetype and mode in self.archetype.modes:
            mode_def = self.archetype.modes[mode]
            constructs = mode_def.get("constructs", [])
            self.active_pack = constructs[0] if constructs else None
            self.active_persona = mode_def.get("persona", "")
            self.secondary_packs = constructs[1:] if len(constructs) > 1 else []
        else:
            self.active_pack = None
            self.active_persona = ""
            self.secondary_packs = []

        if prev:
            self.mode_history.append(f"{prev}→{mode}")
        return True

    def set_construct(self, slug: str) -> bool:
        """Directly activate a construct (not via mode)."""
        if slug == self.active_pack:
            return False
        prev_pack = self.active_pack
        self.active_pack = slug
        self.active_mode = None

        if self.index:
            entry = self.index.packs.get(slug)
            if entry:
                self.active_persona = entry.persona_name or entry.name
            self.secondary_packs = self.index.get_compose_partners(slug)

        if prev_pack:
            self.mode_history.append(f"{prev_pack}→{slug}")
        return True


_sessions: Dict[str, LoaSessionState] = {}


def _get_session(session_id: str) -> LoaSessionState:
    if session_id not in _sessions:
        _sessions[session_id] = LoaSessionState()
    return _sessions[session_id]


# ---------------------------------------------------------------------------
# Construct Loading
# ---------------------------------------------------------------------------

def _load_construct_yaml(pack_name: str) -> Dict[str, Any]:
    """Load construct.yaml/json for a pack."""
    yaml_path = CONSTRUCTS_DIR / pack_name / "construct.yaml"
    json_path = CONSTRUCTS_DIR / pack_name / "construct.json"

    if yaml_path.exists():
        try:
            import yaml
            return yaml.safe_load(yaml_path.read_text()) or {}
        except Exception as e:
            logger.debug("Failed to load %s: %s", yaml_path, e)

    if json_path.exists():
        try:
            return json.loads(json_path.read_text())
        except Exception as e:
            logger.debug("Failed to load %s: %s", json_path, e)

    return {}


def _load_persona_summary(pack_name: str) -> str:
    """Load compact persona from identity files. ≤MAX_PERSONA_CHARS.

    Reads up to 3 sources:
      1. persona.yaml → cognitive frame, voice, personality markers
      2. expertise.yaml → domain names + depth levels
      3. First .md identity file → opening paragraph as essence

    Design: extract scalar fields only. Never dump raw YAML dicts
    into the system prompt — that's the persona YAML dump trap.
    """
    identity_dir = CONSTRUCTS_DIR / pack_name / "identity"
    if not identity_dir.exists():
        return ""

    parts: List[str] = []

    # 1. persona.yaml — cognitive frame + voice
    persona_file = identity_dir / "persona.yaml"
    if persona_file.exists():
        try:
            import yaml
            data = yaml.safe_load(persona_file.read_text()) or {}

            name = data.get("name", pack_name)
            parts.append(f"Construct: {name}")

            cf = data.get("cognitiveFrame", {})
            if cf:
                for key in ("archetype", "disposition", "thinking_style"):
                    val = cf.get(key)
                    if val:
                        label = key.replace("_", " ").title()
                        parts.append(f"{label}: {val}")

            voice = data.get("voice", {})
            if voice:
                for key in ("tone", "register"):
                    val = voice.get(key)
                    if val:
                        parts.append(f"{key.title()}: {val}")
                markers = voice.get("personality_markers", [])
                if markers:
                    parts.append("Markers: " + "; ".join(
                        str(m).rstrip('"').rstrip("'") for m in markers[:4]
                    ))
        except Exception as e:
            logger.debug("persona.yaml parse failed for %s: %s", pack_name, e)

    # 2. expertise.yaml — domain names + levels only
    expertise_file = identity_dir / "expertise.yaml"
    if expertise_file.exists():
        try:
            import yaml
            data = yaml.safe_load(expertise_file.read_text()) or {}
            domains = data.get("domains", data.get("specializations", []))
            if domains and isinstance(domains, list):
                domain_names = []
                for d in domains[:6]:
                    if isinstance(d, dict):
                        name = d.get("name", "")
                        depth = d.get("depth", "")
                        if name:
                            domain_names.append(f"{name} (L{depth})" if depth else name)
                    elif isinstance(d, str):
                        domain_names.append(d)
                if domain_names:
                    parts.append("Expertise: " + ", ".join(domain_names))
        except Exception:
            pass

    # 3. Primary .md identity file — first 4 lines of content
    for md_file in sorted(identity_dir.glob("*.md")):
        if md_file.name.startswith("."):
            continue
        try:
            content = md_file.read_text()
            lines = [
                l.strip() for l in content.split("\n")
                if l.strip()
                and not l.startswith("#")
                and not l.startswith(">")
                and not l.startswith("---")
            ]
            if lines:
                essence = " ".join(lines[:4])
                if len(essence) > 400:
                    essence = essence[:400] + "…"
                parts.append(f"[{md_file.stem}]: {essence}")
                break
        except Exception:
            pass

    result = "\n".join(parts)
    if len(result) > MAX_PERSONA_CHARS:
        result = result[:MAX_PERSONA_CHARS] + "\n[…truncated]"
    return result


# ---------------------------------------------------------------------------
# Activation Detection — L2 from RFC #452
# ---------------------------------------------------------------------------

def _detect_activation(
    user_message: str,
    index: ConstructIndex,
    archetype: ArchetypeConfig,
) -> Tuple[Optional[str], Optional[str]]:
    """Detect what to activate from user message.

    Returns (mode_or_none, pack_slug_or_none).

    Priority:
      1. Explicit mode: "DIG mode" (only if user has defined DIG)
      2. Direct construct: "load kansei"
      3. Implicit vocab: 2+ vocabulary matches from user-defined triggers
    """
    if not user_message:
        return None, None

    # 1. Explicit mode (from user-defined modes only)
    explicit_pattern = archetype.build_explicit_mode_pattern()
    if explicit_pattern:
        explicit = explicit_pattern.search(user_message)
        if explicit:
            mode = explicit.group(1).upper()
            if mode in archetype.modes:
                constructs = archetype.modes[mode].get("constructs", [])
                pack = constructs[0] if constructs else None
                return mode, pack

    # 2. Direct construct reference: "load kansei", "use rosenzu"
    direct = DIRECT_CONSTRUCT.search(user_message)
    if direct:
        name = direct.group(1).lower()
        if name in index.packs:
            return None, name
        for slug in index.packs:
            if name in slug or slug in name:
                return None, slug

    # 3. Implicit mode via user-defined vocabulary (2+ matches)
    scores: Dict[str, int] = {}
    for mode_name, pattern in archetype.mode_triggers.items():
        matches = pattern.findall(user_message)
        if matches:
            scores[mode_name] = len(matches)

    if scores:
        best = max(scores, key=scores.get)
        if scores[best] >= 2:
            constructs = archetype.modes.get(best, {}).get("constructs", [])
            pack = constructs[0] if constructs else None
            return best, pack

    return None, None


# ---------------------------------------------------------------------------
# Context Building
# ---------------------------------------------------------------------------

def _build_context_injection(state: LoaSessionState) -> Optional[str]:
    """Build the context string to inject for current state."""
    if not state.active_pack:
        return None

    parts = []

    if state.active_mode:
        parts.append(
            f"[Loa: {state.active_persona or state.active_pack} "
            f"({state.active_mode} mode)]"
        )
    else:
        parts.append(
            f"[Loa: {state.active_persona or state.active_pack}]"
        )

    if state.persona_context:
        parts.append(state.persona_context)

    # Composition partners (L3: Composition as Pipe)
    if state.secondary_packs and state.index:
        partners = []
        for slug in state.secondary_packs[:3]:
            entry = state.index.packs.get(slug)
            if entry:
                partners.append(f"{entry.name}: {entry.short_description}")
        if partners:
            parts.append("[Composes with: " + " | ".join(partners) + "]")

    if state.mode_history:
        parts.append(f"[Session path: {' → '.join(state.mode_history[-3:])}]")

    return "\n".join(parts)


def _build_session_greeting(state: LoaSessionState) -> str:
    """Build L5-style ambient awareness greeting for session start.

    Shows active constructs and available modes so the user knows
    what's loaded without having to ask.
    """
    parts = []

    if state.index and state.index.packs:
        pack_count = len(state.index.packs)
        parts.append(f"[Loa: {pack_count} constructs indexed]")

    if state.archetype and state.archetype.modes:
        mode_names = ", ".join(sorted(state.archetype.modes.keys()))
        parts.append(f"[Modes: {mode_names}]")

    if state.archetype and state.archetype.active_constructs:
        active = ", ".join(state.archetype.active_constructs)
        parts.append(f"[Active: {active}]")

    return "\n".join(parts) if parts else ""


def _load_extensions(
    state: LoaSessionState, user_message: str
) -> Optional[str]:
    """Extension point for custom context injections.

    Override this function to add domain-specific context loading.
    Examples: organizational memory, cwd-based project context,
    or external knowledge base queries.

    Return a string to inject, or None.
    """
    return None


# ---------------------------------------------------------------------------
# Hook Implementations
# ---------------------------------------------------------------------------

def _on_session_start(session_id: str, model: str = "", platform: str = "", **kw):
    """Index all construct packs and load user archetype."""
    state = _get_session(session_id)

    # L1: Build Construct Index
    state.index = ConstructIndex()
    state.index.build()

    # L4: Load user-defined archetype
    state.archetype = ArchetypeConfig()
    state.archetype.load()

    logger.info(
        "Loa: session %s — %d packs, %d modes",
        session_id[:8],
        len(state.index.packs),
        len(state.archetype.modes),
    )


def _pre_llm_call(
    session_id: str,
    user_message: str = "",
    conversation_history: list = None,
    is_first_turn: bool = False,
    model: str = "",
    platform: str = "",
    **kw,
) -> Optional[Dict[str, str]]:
    """Detect and inject construct context before LLM call.

    Returns {"context": "..."} which Hermes appends to the
    ephemeral system prompt for this turn only. Not persisted.
    """
    state = _get_session(session_id)
    state.turn_count += 1

    if not state.index:
        state.index = ConstructIndex()
        state.index.build()
    if not state.archetype:
        state.archetype = ArchetypeConfig()
        state.archetype.load()

    context_parts = []

    # L5: Session greeting on first turn
    if is_first_turn:
        greeting = _build_session_greeting(state)
        if greeting:
            context_parts.append(greeting)

    # L2: Detect activation (mode or direct construct)
    mode, pack = _detect_activation(user_message, state.index, state.archetype)

    if mode:
        changed = state.set_mode(mode)
        if changed and state.active_pack:
            state.persona_context = _load_persona_summary(state.active_pack)
            logger.info("Loa: mode=%s pack=%s persona=%s",
                        mode, state.active_pack, state.active_persona)
    elif pack:
        changed = state.set_construct(pack)
        if changed:
            state.persona_context = _load_persona_summary(pack)
            logger.info("Loa: direct construct=%s persona=%s",
                        pack, state.active_persona)

    # Build persona injection
    construct_ctx = _build_context_injection(state)
    if construct_ctx:
        context_parts.append(construct_ctx)

    # Extension point
    ext_ctx = _load_extensions(state, user_message)
    if ext_ctx:
        context_parts.append(ext_ctx)

    if context_parts:
        return {"context": "\n\n".join(context_parts)}
    return None


def _post_llm_call(
    session_id: str,
    user_message: str = "",
    assistant_response: str = "",
    conversation_history: list = None,
    model: str = "",
    platform: str = "",
    **kw,
):
    """Observe responses for mode transition signals."""
    if not assistant_response:
        return

    state = _get_session(session_id)
    if not state.archetype or not state.archetype.modes:
        return

    # Build dynamic pattern from user-defined modes
    mode_names = "|".join(re.escape(m) for m in state.archetype.modes.keys())
    patterns = [
        rf"this\s+(?:sounds?\s+like|is)\s+({mode_names})\s+territory",
        rf"switching\s+to\s+({mode_names})\s+mode",
        rf"({mode_names})\s+mode\s+(?:activated|engaged)",
    ]

    for pattern in patterns:
        match = re.search(pattern, assistant_response, re.I)
        if match:
            suggested = match.group(1).upper()
            if suggested != state.active_mode:
                logger.info("Loa: assistant signaled %s (current: %s)",
                            suggested, state.active_mode)
                break


def _on_session_end(session_id: str, **kw):
    """Clean up session state."""
    state = _sessions.pop(session_id, None)
    if state and state.turn_count > 0:
        logger.info(
            "Loa: session %s — %d turns, transitions: %s",
            session_id[:8], state.turn_count,
            " → ".join(state.mode_history) if state.mode_history else "(none)",
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(ctx):
    """Register Loa hooks with Hermes."""
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("post_llm_call", _post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
    logger.info("Loa Constructs plugin registered")
