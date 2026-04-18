# -*- coding: utf-8 -*-
"""View-related tools for capturing and listing Revit views"""

from typing import Optional
from mcp.server.fastmcp import Context
from .utils import format_response


def register_view_tools(mcp, revit_get, revit_post, revit_image):
    """Register view-related tools"""

    @mcp.tool()
    async def get_revit_view(
        view_name: str,
        ctx: Context = None,
        instance: Optional[str] = None,
    ):
        """Export a specific Revit view as an image.

        Args:
            view_name: Name of the view to export.
            instance: Revit version year to target. See list_revit_instances.
        """
        return await revit_image(f"/get_view/{view_name}", ctx, instance=instance)

    @mcp.tool()
    async def list_revit_views(
        ctx: Context = None,
        instance: Optional[str] = None,
    ) -> str:
        """Get a list of all exportable views in the current Revit model.

        Args:
            instance: Revit version year to target. See list_revit_instances.
        """
        response = await revit_get("/list_views/", ctx, instance=instance)
        return format_response(response)

    @mcp.tool()
    async def get_current_view_info(
        ctx: Context = None,
        instance: Optional[str] = None,
    ) -> str:
        """
        Get detailed information about the currently active view in Revit.

        Returns comprehensive information including:
        - View name, type, and ID
        - Scale and detail level
        - Crop box status
        - View family type
        - View discipline
        - Template status

        Args:
            instance: Revit version year to target. See list_revit_instances.
        """
        if ctx:
            await ctx.info("Getting current view information...")
        response = await revit_get("/current_view_info/", ctx, instance=instance)
        return format_response(response)

    @mcp.tool()
    async def get_current_view_elements(
        limit: int = 5000,
        include_levels: bool = False,
        include_location: bool = False,
        ctx: Context = None,
        instance: Optional[str] = None,
    ) -> str:
        """
        Get elements visible in the currently active view in Revit.

        Returns per element: element_id, name, category, category_id.
        Also returns category_counts (always for ALL elements, even if truncated).

        If the response contains truncated=true, not all elements were returned.
        Check total_elements vs returned_elements and increase limit if needed.

        Args:
            limit: Maximum number of elements to return (default 5000).
            include_levels: Include level name and level_id per element. Default false.
            include_location: Include location geometry (point or curve). Default false.
            instance: Revit version year to target. See list_revit_instances.
        """
        if ctx:
            await ctx.info("Getting elements in current view...")
        data = {
            "limit": limit,
            "include_levels": include_levels,
            "include_location": include_location,
        }
        response = await revit_post(
            "/current_view_elements/", data, ctx, instance=instance
        )
        return format_response(response)
