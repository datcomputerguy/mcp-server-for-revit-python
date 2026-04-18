# -*- coding: utf-8 -*-
"""Multi-instance management tools for Revit MCP Server."""

import json
import os
import subprocess
from typing import Optional

import anyio
from mcp.server.fastmcp import Context

from instance_registry import (
    discover_instances,
    invalidate,
    resolve_port,
    unregister_instance,
)


def _is_windows() -> bool:
    return os.name == "nt"


async def _terminate_process_by_port(port: int, ctx: Context = None) -> dict:
    """Find the process bound to the given port and terminate it.

    Uses netstat + taskkill on Windows; lsof + kill elsewhere. Non-destructive
    in the sense that we don't save the document — Revit will prompt in its
    own UI and the user will have to dismiss it. This tool is intentionally
    no-frills: if you need a graceful close-with-prompts-handled, use the
    close_document tool first, then call this.
    """
    try:
        if _is_windows():
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "tcp"],
                stderr=subprocess.STDOUT,
                text=True,
            )
            pid = None
            for line in out.splitlines():
                parts = line.split()
                if len(parts) < 5:
                    continue
                # Looking for LISTENING rows on 0.0.0.0:<port> or 127.0.0.1:<port>
                local = parts[1]
                state = parts[3]
                if state != "LISTENING":
                    continue
                if local.endswith(":{}".format(port)):
                    pid = parts[-1]
                    break
            if not pid:
                return {
                    "status": "error",
                    "error": "No process found listening on port {}".format(port),
                }
            if ctx:
                await ctx.info("Terminating Revit PID {} on port {}".format(pid, port))
            subprocess.check_call(["taskkill", "/F", "/PID", str(pid)])
            return {"status": "success", "pid": int(pid)}
        else:
            # POSIX fallback
            out = subprocess.check_output(
                ["lsof", "-iTCP:{}".format(port), "-sTCP:LISTEN", "-t"],
                stderr=subprocess.STDOUT,
                text=True,
            )
            pid = out.strip().splitlines()[0] if out.strip() else None
            if not pid:
                return {
                    "status": "error",
                    "error": "No process found listening on port {}".format(port),
                }
            os.kill(int(pid), 15)  # SIGTERM
            return {"status": "success", "pid": int(pid)}
    except subprocess.CalledProcessError as e:
        return {"status": "error", "error": str(e), "output": getattr(e, "output", "")}
    except FileNotFoundError as e:
        return {"status": "error", "error": "Missing system tool: {}".format(e)}


def register_instance_tools(mcp):
    """Register multi-instance discovery + management tools."""

    @mcp.tool()
    async def list_revit_instances(
        ctx: Context = None,
        refresh: bool = False,
    ) -> str:
        """List all running Revit instances reachable via pyRevit Routes.

        Scans ports 48884-48894 and reports each detected instance with its
        Revit version, pyRevit Routes port, and currently-active document
        title. Use the returned version strings as the `instance` parameter
        on other tools to target a specific Revit.

        Args:
            refresh: If True, force a port rescan instead of using the cached
                registry. Useful right after launching or closing an instance.
        """
        if refresh:
            await invalidate()
        try:
            registry = await discover_instances(force=refresh)
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)}, indent=2)

        instances = sorted(
            registry.values(),
            key=lambda i: str(i.get("version") or ""),
            reverse=True,
        )
        default_version = None
        if instances:
            # Match the auto-pick rule used by resolve_port(): latest version
            # when >1, otherwise the only one.
            default_version = str(instances[0].get("version"))

        return json.dumps(
            {
                "status": "success",
                "count": len(instances),
                "default_version_if_unspecified": default_version,
                "instances": instances,
            },
            indent=2,
        )

    @mcp.tool()
    async def close_revit_instance(
        ctx: Context,
        instance: Optional[str] = None,
        force: bool = False,
    ) -> str:
        """Terminate a running Revit instance by version year.

        This kills the Revit process. If a document is unsaved, Revit's
        native save-prompt will appear and the user must dismiss it —
        this tool does not auto-confirm. For a graceful close, use
        close_document first and then this tool.

        Args:
            instance: Revit version year (e.g. "2024"). If omitted, closes
                the default instance (same resolution rule as other tools).
            force: If False, this tool requires the `instance` parameter
                to be explicit when multiple Revits are running (safety
                guard to prevent accidentally closing the wrong one).
                Set True to allow auto-selection even with multiple instances.
        """
        registry = await discover_instances()
        if not registry:
            return json.dumps(
                {"status": "error", "error": "No Revit instances running."},
                indent=2,
            )

        if not instance and not force and len(registry) > 1:
            return json.dumps(
                {
                    "status": "error",
                    "error": (
                        "Multiple Revit instances running; refusing to auto-"
                        "select for a close operation. Pass instance=\"YEAR\" "
                        "or force=True."
                    ),
                    "available": sorted(registry.keys()),
                },
                indent=2,
            )

        try:
            port = await resolve_port(instance)
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)}, indent=2)

        # Work out which version we're killing for the response
        target_version = None
        for v, info in registry.items():
            if int(info.get("port", -1)) == port:
                target_version = v
                break

        result = await _terminate_process_by_port(port, ctx=ctx)
        if result.get("status") == "success" and target_version:
            await unregister_instance(target_version)

        return json.dumps(
            {
                "status": result.get("status"),
                "closed_version": target_version,
                "port": port,
                "pid": result.get("pid"),
                "error": result.get("error"),
            },
            indent=2,
        )
