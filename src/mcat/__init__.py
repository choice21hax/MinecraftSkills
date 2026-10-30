"""Minecraft Agent Toolkit (MCAT): deterministic engine behind the minecraft-* agent skills."""

import os

__version__ = "1.0.0rc2"


def user_agent() -> str:
    """Descriptive User-Agent as requested by the Modrinth API docs. Set MCAT_CONTACT to add contact info."""
    contact = os.environ.get("MCAT_CONTACT", "internal tool")
    return f"mcat/minecraft-agent-toolkit/{__version__} ({contact})"
