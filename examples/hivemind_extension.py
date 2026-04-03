"""
Example: Organizational Memory Extension
=========================================

Shows how to extend the Loa plugin with domain-specific context injection.
This pattern detects when a user asks organizational questions and
auto-injects relevant knowledge files from a construct pack.

To use: replace the _load_extensions function in __init__.py with
a call to org_memory_extension().

This is NOT required for core Loa functionality — it's a reference
for building your own context-aware injections.
"""

import os
import re
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hermes.plugins.loa")

CONSTRUCTS_DIR = Path(os.environ.get(
    "LOA_HOME", os.path.expanduser("~/.loa")
)) / "constructs" / "packs"


# ---------------------------------------------------------------------------
# Configuration — customize for your organization
# ---------------------------------------------------------------------------

# The construct pack that holds organizational memory
ORG_MEMORY_PACK = "hivemind-os"

# Regex patterns that detect organizational knowledge needs
ORG_TRIGGERS = re.compile(
    r"(?:"
    r"\bwhat\s+(?:products?|repos?|services?|teams?)\b|"
    r"\bwho\s+(?:owns?|knows?|built|maintains?)\b|"
    r"\bwhere\s+.*\b(?:deployed|running|hosted)\b|"
    r"\bwhat\s+(?:do\s+we\s+call|is\s+the\s+term)\b|"
    r"\bADR\b|architecture\s+decision|"
    r"\bwhy\s+did\s+we\s+(?:decide|choose|pick)\b|"
    r"\becosystem\s+(?:overview|map|status)\b"
    r")",
    re.IGNORECASE,
)

# Map knowledge areas to file paths within the org memory pack
KNOWLEDGE_FILES = {
    "products":    "laboratory/registry/PRODUCTS.yaml",
    "repos":       "library/ecosystem/REPOS.md",
    "ecosystem":   "library/ecosystem/OVERVIEW.md",
    "terminology": "library/TERMINOLOGY.md",
    "team":        "library/team/INDEX.md",
    "deployments": "laboratory/infrastructure/DEPLOYMENTS.md",
    "decisions":   "laboratory/decisions/INDEX.md",
    "contracts":   "library/contracts/REGISTRY.md",
}


# ---------------------------------------------------------------------------
# Detection & Loading
# ---------------------------------------------------------------------------

def _detect_need(user_message: str) -> Optional[str]:
    """Detect if the user needs organizational knowledge."""
    if not user_message or not ORG_TRIGGERS.search(user_message):
        return None

    msg = user_message.lower()
    routing = [
        (("product", "registry"), "products"),
        (("repo", "repository", "codebase"), "repos"),
        (("who ", "team", "owns", "maintains"), "team"),
        (("deploy", "infra", "hosted", "running"), "deployments"),
        (("adr", "decision", "why did"), "decisions"),
        (("term", "call", "glossary"), "terminology"),
        (("contract", "address", "multisig"), "contracts"),
        (("ecosystem", "overview"), "ecosystem"),
    ]

    for keywords, area in routing:
        if any(w in msg for w in keywords):
            return area

    return "ecosystem"


def _load_context(hint: str) -> str:
    """Load organizational context, capped for prompt injection."""
    full_path = CONSTRUCTS_DIR / ORG_MEMORY_PACK / KNOWLEDGE_FILES.get(hint, "")
    if not full_path.exists():
        return ""

    try:
        content = full_path.read_text()

        if full_path.suffix in (".yaml", ".yml"):
            if len(content) > 1500:
                content = content[:1500] + "\n# …[truncated]"
            return f"[Org Memory — {hint}]\n{content}"

        # Markdown: extract headers + list items
        lines = content.split("\n")
        summary, chars = [], 0
        for line in lines:
            if chars > 1200:
                summary.append(f"\n[…truncated — {full_path}]")
                break
            s = line.strip()
            if s.startswith("#") or s.startswith("|") or s.startswith("-") or not s:
                summary.append(line)
                chars += len(line)

        return f"[Org Memory — {hint}]\n" + "\n".join(summary)

    except Exception as e:
        logger.debug("Failed to load org memory %s: %s", hint, e)
        return ""


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def org_memory_extension(state, user_message: str) -> Optional[str]:
    """Drop-in replacement for _load_extensions in __init__.py.

    Usage — edit __init__.py:

        from examples.hivemind_extension import org_memory_extension

        def _load_extensions(state, user_message):
            return org_memory_extension(state, user_message)
    """
    hint = _detect_need(user_message)
    if hint:
        logger.info("Loa: org memory triggered — %s", hint)
        return _load_context(hint)
    return None
