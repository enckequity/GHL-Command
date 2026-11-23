"""GHL Command: GoHighLevel tools for an agent.

Native GoHighLevel tools. Contacts, pipelines,
calendars, workflows, email and SMS sends directly from chat.
"""

from __future__ import annotations

import logging

try:  # package import (plugin loader)
    from .tools import register_tools, TOOLSET
except ImportError:  # file import (pytest collection)
    from tools import register_tools, TOOLSET

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Plugin entry point, called by the plugin loader."""
    register_tools(ctx)
    logger.info("ghl-command: registered GHL tools in toolset %s", TOOLSET)


__all__ = ["register", "register_tools", "TOOLSET"]
