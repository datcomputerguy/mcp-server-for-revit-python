# -*- coding: utf-8 -*-
"""Model structure and hierarchy tools"""

from typing import Optional
from mcp.server.fastmcp import Context
from .utils import format_response


def register_model_tools(mcp, revit_get):
    """Register model structure tools"""

    @mcp.tool()
    async def list_levels(
        ctx: Context = None,
        instance: Optional[str] = None,
    ) -> str:
        """Get a list of all levels in the current Revit model.

        Args:
            instance: Revit version year to target (e.g. "2024", "2025").
                See list_revit_instances.
        """
        response = await revit_get("/list_levels/", ctx, instance=instance)
        return format_response(response)
