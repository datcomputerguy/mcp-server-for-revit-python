# -*- coding: utf-8 -*-
"""Launch and discovery tools for Revit instances"""

import os
import subprocess
import json
import time
from typing import Optional

import anyio
from mcp.server.fastmcp import Context
from .utils import format_response

from instance_registry import discover_instances, register_instance, invalidate


def _find_revit_installations():
    """Scan the system for installed Revit versions.

    Checks Windows Registry and common filesystem paths.
    Returns a list of {"year": str, "path": str} sorted newest-first.
    """
    found = {}

    # Strategy 1: Windows Registry
    try:
        import winreg

        base_key_path = r"SOFTWARE\Autodesk\Revit"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                base_key = winreg.OpenKey(hive, base_key_path)
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(base_key, i)
                        i += 1
                        # Subkeys are often like "Autodesk Revit 2025"
                        # Extract the year from the subkey name
                        year = None
                        for token in subkey_name.split():
                            if token.isdigit() and len(token) == 4:
                                year = token
                                break

                        if not year:
                            continue

                        subkey = winreg.OpenKey(base_key, subkey_name)
                        # Try common value names for install path
                        for value_name in (
                            "InstallationLocation",
                            "InstallPath",
                            "",
                        ):
                            try:
                                val, _ = winreg.QueryValueEx(
                                    subkey, value_name
                                )
                                if val and os.path.isdir(val):
                                    exe = os.path.join(val, "Revit.exe")
                                    if os.path.isfile(exe):
                                        found[year] = exe
                                    break
                            except OSError:
                                continue
                        winreg.CloseKey(subkey)
                    except OSError:
                        break
                winreg.CloseKey(base_key)
            except OSError:
                continue
    except ImportError:
        pass  # Not on Windows

    # Strategy 2: Filesystem fallback
    program_files = os.environ.get(
        "ProgramFiles", r"C:\Program Files"
    )
    for year in range(2027, 2019, -1):
        year_str = str(year)
        if year_str in found:
            continue
        exe = os.path.join(
            program_files, "Autodesk", "Revit {}".format(year_str), "Revit.exe"
        )
        if os.path.isfile(exe):
            found[year_str] = exe

    # Sort newest-first
    installations = [
        {"year": y, "path": p}
        for y, p in sorted(found.items(), key=lambda x: x[0], reverse=True)
    ]
    return installations


def _select_revit(installations, version=None):
    """Pick a Revit installation by version year, or the latest available."""
    if not installations:
        return None
    if version:
        for inst in installations:
            if inst["year"] == str(version):
                return inst
        return None
    return installations[0]


def _build_launch_command(revit_path, file_path=None, language=None):
    """Construct the subprocess argument list for launching Revit."""
    args = [revit_path]
    if language:
        args.extend(["/language", language])
    if file_path:
        args.append(file_path)
    return args


async def _wait_for_instance_ready(
    target_version: str,
    ctx: Optional[Context],
    timeout: int,
    poll_interval: int = 5,
    require_document: bool = False,
):
    """Poll the instance registry until the target version shows up.

    Re-scans every poll_interval seconds (forcing registry refresh so newly-
    launched Revits are discovered). Returns (found, info) where `info` is
    the registry entry for the new instance, or (False, None) on timeout.

    When require_document=True, also waits for a non-null document_title so
    we only return after the requested file has actually finished opening.
    """
    start = time.time()
    while time.time() - start < timeout:
        elapsed = int(time.time() - start)
        if ctx:
            await ctx.info(
                "Waiting for Revit {} to be ready... ({}s / {}s)".format(
                    target_version, elapsed, timeout
                )
            )
        try:
            registry = await discover_instances(force=True)
            # Registry is keyed by port; find the lowest-port entry matching
            # our target version. When launching a second same-year instance
            # we may briefly see the older one first — the fresh instance
            # shows up on a higher port a moment later.
            matches = sorted(
                (
                    info for info in registry.values()
                    if str(info.get("version")) == str(target_version)
                ),
                key=lambda i: int(i.get("port", 0)),
            )
            for info in matches:
                if not require_document:
                    return True, info
                if info.get("document_title"):
                    return True, info
        except Exception:
            # Registry scan failed — keep retrying until timeout
            pass
        await anyio.sleep(poll_interval)
    return False, None


def register_launch_tools(mcp, revit_get):
    """Register Revit launch and discovery tools with the MCP server."""

    @mcp.tool()
    async def list_revit_installations(ctx: Context) -> str:
        """Discover all Revit versions installed on this system.

        Returns a list of installed Revit versions with their executable paths.
        Use this to check what's available before calling launch_revit.
        """
        try:
            installations = _find_revit_installations()
        except Exception as e:
            return json.dumps(
                {"status": "error", "error": str(e)}, indent=2
            )

        if not installations:
            return json.dumps(
                {
                    "status": "success",
                    "installations": [],
                    "message": "No Revit installations found. "
                    "Checked Windows Registry and common install paths.",
                },
                indent=2,
            )

        return json.dumps(
            {
                "status": "success",
                "installations": installations,
                "count": len(installations),
            },
            indent=2,
        )

    @mcp.tool()
    async def launch_revit(
        ctx: Context,
        file_path: str = None,
        version: str = None,
        language: str = None,
        timeout: int = 600,
        wait_for_document: bool = None,
    ) -> str:
        """Launch Revit on this machine, optionally opening a file.

        Finds installed Revit versions automatically. After launching, polls
        the instance registry until the new Revit shows up (and, when a
        file was specified, until its document has finished loading). Cloud
        models can take several minutes to download on first open.

        For workshared (central model) files, Revit will show its native
        worksharing dialog on open. Use the open_document tool after launch
        for more control over worksharing options like detach from central.

        Args:
            file_path: Path to a .rvt, .rfa, or .rte file to open. Optional.
            version: Revit version year (e.g. "2025"). Uses latest if omitted.
            language: Language code (e.g. "ENU", "FRA"). Optional.
            timeout: Seconds to wait for Revit readiness (default 600 = 10 min,
                generous to cover cloud model downloads).
            wait_for_document: Force wait-until-document-loaded behavior. If
                None (default), auto-detects: True when file_path is provided,
                False otherwise.
        """
        # Validate file path if provided
        if file_path:
            if not os.path.isfile(file_path):
                return json.dumps(
                    {
                        "status": "error",
                        "error": "File not found: {}".format(file_path),
                    },
                    indent=2,
                )
            ext = os.path.splitext(file_path)[1].lower()
            if ext not in (".rvt", ".rfa", ".rte"):
                return json.dumps(
                    {
                        "status": "error",
                        "error": "Unsupported file type '{}'. "
                        "Expected .rvt, .rfa, or .rte".format(ext),
                    },
                    indent=2,
                )

        # Find installations
        try:
            installations = _find_revit_installations()
        except Exception as e:
            return json.dumps(
                {
                    "status": "error",
                    "error": "Failed to scan for Revit installations: {}".format(
                        str(e)
                    ),
                },
                indent=2,
            )

        if not installations:
            return json.dumps(
                {
                    "status": "error",
                    "error": "No Revit installations found on this system.",
                },
                indent=2,
            )

        # Select version
        selected = _select_revit(installations, version)
        if not selected:
            available = ", ".join(i["year"] for i in installations)
            return json.dumps(
                {
                    "status": "error",
                    "error": "Revit {} not found. Available versions: {}".format(
                        version, available
                    ),
                },
                indent=2,
            )

        # Build and launch
        cmd = _build_launch_command(
            selected["path"], file_path, language
        )

        try:
            subprocess.Popen(cmd)
        except OSError as e:
            return json.dumps(
                {
                    "status": "error",
                    "error": "Failed to launch Revit: {}".format(str(e)),
                    "attempted_path": selected["path"],
                },
                indent=2,
            )

        target_version = selected["year"]
        if ctx:
            await ctx.info(
                "Revit {} launched. Waiting for pyRevit Routes registry "
                "to detect the new instance...".format(target_version)
            )

        if wait_for_document is None:
            wait_for_document = bool(file_path)

        # Drop any cached registry so the next scan picks up the new instance
        await invalidate()

        ready, info = await _wait_for_instance_ready(
            target_version,
            ctx,
            timeout,
            require_document=wait_for_document,
        )

        result = {
            "status": "success" if ready else "timeout",
            "revit_version": target_version,
            "revit_path": selected["path"],
            "file_opened": file_path,
            "revit_ready": ready,
            "wait_for_document": wait_for_document,
        }

        if ready and info:
            result["port"] = info["port"]
            result["document_title"] = info.get("document_title")
            result["message"] = (
                "Revit {} is running on port {}. Use instance=\"{}\" on "
                "other tools to target it.".format(
                    target_version, info["port"], target_version
                )
            )
            # Make sure the registry has it under the exact version string
            await register_instance(
                target_version,
                int(info["port"]),
                info.get("document_title"),
            )
        else:
            result["message"] = (
                "Revit {} was launched but the registry did not pick it up "
                "within {} seconds. Ensure pyRevit is installed, Routes "
                "Server is enabled in pyRevit Settings, and call "
                "list_revit_instances with refresh=True to re-probe.".format(
                    target_version, timeout
                )
            )

        if file_path:
            result["worksharing_note"] = (
                "If this is a workshared (central) file, Revit will show its "
                "native dialog for creating a local copy. For programmatic "
                "control over worksharing options (detach, audit), use the "
                "open_document tool after Revit is ready."
            )

        return json.dumps(result, indent=2)
