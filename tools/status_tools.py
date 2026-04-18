# -*- coding: utf-8 -*-
"""Status and model information tools"""

from typing import Optional
from mcp.server.fastmcp import Context
from .utils import format_response


def register_status_tools(mcp, revit_get):
    """Register status-related tools"""

    @mcp.tool()
    async def get_revit_status(
        ctx: Context,
        instance: Optional[str] = None,
    ) -> str:
        """Check if the Revit MCP API is active and responding.

        Args:
            instance: Revit version year to target (e.g. "2024", "2025").
                If omitted, uses the single running instance, or the newest
                version when multiple are running. See list_revit_instances.
        """
        response = await revit_get("/status/", ctx, instance=instance, timeout=10.0)
        return format_response(response)

    @mcp.tool()
    async def get_revit_model_info(
        ctx: Context,
        instance: Optional[str] = None,
    ) -> str:
        """Get comprehensive information about the current Revit model.

        Args:
            instance: Revit version year to target (e.g. "2024", "2025").
                See list_revit_instances.
        """
        response = await revit_get("/model_info/", ctx, instance=instance)
        return format_response(response)
