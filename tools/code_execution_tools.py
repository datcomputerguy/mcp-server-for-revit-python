# -*- coding: utf-8 -*-
"""Code execution tools for the MCP server."""

from typing import Optional
from mcp.server.fastmcp import Context
from .utils import format_response

# Seconds to wait for /execute_code/ before giving up. The wait is NOT a
# cancel: when it expires the request is abandoned here but the code carries on
# running inside Revit (see the timeout message in main._revit_call). So a
# longer wait does not buy safety -- it buys patience, and Revit's UI thread is
# blocked for the whole of it.
DEFAULT_CODE_TIMEOUT = 60.0
# Hard ceiling on the caller-supplied override. Past this the right answer is a
# smaller query, not a longer wait: Revit is frozen the entire time and a user
# watching a five-minute hang has no way to tell it from a crash.
MAX_CODE_TIMEOUT = 300.0


def register_code_execution_tools(mcp, revit_get, revit_post, revit_image=None):
    """Register code execution tools with the MCP server."""
    # Note: revit_get and revit_image are unused but kept for interface consistency
    _ = revit_get, revit_image  # Acknowledge unused parameters

    @mcp.tool()
    async def execute_revit_code(
        code: str,
        description: str = "Code execution",
        instance: Optional[str] = None,
        timeout: Optional[float] = None,
        ctx: Context = None,
    ) -> str:
        """
        Execute IronPython code directly in Revit context.

        The code has access to:
        - doc: The active Revit document
        - uidoc: The active UIDocument (use for UI operations like switching the active view)
        - DB: Revit API Database namespace
        - revit: pyRevit module
        - print: Function to output text (returned in response)

        No transaction is opened automatically. Wrap model-modifying code yourself:
            t = DB.Transaction(doc, "My change")
            t.Start()
            # ... modify model ...
            t.Commit()

        For UI operations that cannot run inside a transaction (e.g. switching the active view):
            all_views = DB.FilteredElementCollector(doc).OfClass(DB.View).ToElements()
            target = next((v for v in all_views if v.Name == "Level 1"), None)
            if target:
                uidoc.ActiveView = target

        Tips:
        - Use getattr(element, 'Name', 'N/A') to safely access the Name property
        - Check elements exist before use: if element:
        - Use hasattr() for optional properties

        Args:
            code: Python (IronPython) code to execute inside Revit.
            description: Short label for logs/output.
            instance: Revit version year (e.g. "2024", "2025") to target when
                multiple Revits are running. If omitted, uses the single active
                instance, or the newest version if more than one. Call
                list_revit_instances to see what's available.
            timeout: Seconds to wait, default 60, capped at 300. Raise it ONLY
                for an operation already known to be slow and already scoped
                down as far as it goes.

                Understand what it does before using it. The code runs on
                Revit's UI thread, so Revit is frozen for the whole wait -- a
                180-second timeout is a three-minute freeze the user cannot
                tell apart from a crash. And the wait does not cancel
                anything: when it expires this request is abandoned but the
                code keeps running inside Revit. A longer timeout therefore
                buys patience, never safety, and never a clean abort.

                A slow read usually means the query is too broad. Narrow it
                first; reach for this only when it genuinely cannot be.
        """
        try:
            payload = {"code": code, "description": description}

            if timeout is None:
                wait = DEFAULT_CODE_TIMEOUT
            else:
                wait = max(1.0, min(float(timeout), MAX_CODE_TIMEOUT))

            if ctx:
                await ctx.info("Executing code: {}".format(description))
                if wait > DEFAULT_CODE_TIMEOUT:
                    # Say it out loud: Revit is about to be unresponsive for
                    # longer than anyone expects, and that should be on record.
                    await ctx.info(
                        "Extended timeout: waiting up to {:.0f}s. Revit's UI "
                        "thread is blocked for the duration.".format(wait)
                    )

            response = await revit_post(
                "/execute_code/", payload, ctx, instance=instance, timeout=wait
            )
            return format_response(response)

        except (ConnectionError, ValueError, RuntimeError) as e:
            error_msg = "Error during code execution: {}".format(str(e))
            if ctx:
                await ctx.error(error_msg)
            return error_msg
