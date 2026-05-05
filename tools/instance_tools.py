# -*- coding: utf-8 -*-
"""Multi-instance management tools for Revit MCP Server."""

import json
import os
import subprocess
import time
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


def _find_pid_on_port(port: int) -> Optional[str]:
    """Return the PID (string) listening on the given TCP port, or None."""
    try:
        if _is_windows():
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "tcp"],
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in out.splitlines():
                parts = line.split()
                if len(parts) < 5:
                    continue
                local = parts[1]
                state = parts[3]
                if state != "LISTENING":
                    continue
                if local.endswith(":{}".format(port)):
                    return parts[-1]
            return None
        else:
            out = subprocess.check_output(
                ["lsof", "-iTCP:{}".format(port), "-sTCP:LISTEN", "-t"],
                stderr=subprocess.STDOUT,
                text=True,
            )
            pid = out.strip().splitlines()[0] if out.strip() else None
            return pid
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _pid_is_alive(pid: str) -> bool:
    """Return True if a process with the given PID is still running."""
    if not pid:
        return False
    try:
        if _is_windows():
            out = subprocess.check_output(
                ["tasklist", "/FI", "PID eq {}".format(pid), "/FO", "CSV", "/NH"],
                stderr=subprocess.STDOUT,
                text=True,
            )
            # tasklist prints "INFO: No tasks are running..." when not found.
            # When found, the CSV row starts with the image name in quotes.
            return '"' in out and "INFO" not in out.upper()
        else:
            os.kill(int(pid), 0)
            return True
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
        return False


async def _graceful_terminate_by_port(
    port: int, ctx: Context = None, timeout_seconds: float = 20.0
) -> dict:
    """Send WM_CLOSE to the Revit process on `port` and wait for it to exit.

    This is the polite path: `taskkill /PID <pid>` (no `/F`) on Windows or
    SIGTERM on POSIX. If there are unsaved documents, Revit's native save
    prompt will appear and the process will NOT exit until the user (or
    some other caller) dismisses it. Use close_document or save_document
    BEFORE calling this so Revit has nothing dirty to prompt about.

    Returns a dict with keys:
        status:  "success" | "still_running" | "error"
        pid:     the PID we targeted (int) or None
        elapsed: seconds we waited
        error:   optional message
    """
    pid = _find_pid_on_port(port)
    if not pid:
        return {
            "status": "error",
            "pid": None,
            "elapsed": 0.0,
            "error": "No process found listening on port {}".format(port),
        }

    if ctx:
        await ctx.info(
            "Gracefully terminating Revit PID {} on port {} "
            "(waiting up to {:.0f}s)".format(pid, port, timeout_seconds)
        )

    try:
        if _is_windows():
            # taskkill without /F sends WM_CLOSE on Windows.
            subprocess.check_call(
                ["taskkill", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            os.kill(int(pid), 15)  # SIGTERM
    except subprocess.CalledProcessError as e:
        return {
            "status": "error",
            "pid": int(pid),
            "elapsed": 0.0,
            "error": "Failed to send graceful terminate: {}".format(e),
        }

    # Poll for exit
    start = time.time()
    poll_interval = 0.5
    while True:
        elapsed = time.time() - start
        if not _pid_is_alive(pid):
            return {
                "status": "success",
                "pid": int(pid),
                "elapsed": round(elapsed, 1),
            }
        if elapsed >= timeout_seconds:
            return {
                "status": "still_running",
                "pid": int(pid),
                "elapsed": round(elapsed, 1),
                "error": (
                    "Revit PID {} still running after {:.0f}s. "
                    "Likely blocked on a save/sync prompt. "
                    "Dismiss it manually or retry with save_mode='force'."
                ).format(pid, elapsed),
            }
        await anyio.sleep(poll_interval)


async def _hard_terminate_by_port(port: int, ctx: Context = None) -> dict:
    """Kill the Revit process bound to `port` with extreme prejudice.

    Windows uses `taskkill /F`, POSIX uses SIGKILL. Use only when graceful
    close has already timed out OR when the caller has explicitly opted in
    via save_mode='force'. Unsaved work WILL be lost.
    """
    pid = _find_pid_on_port(port)
    if not pid:
        return {
            "status": "error",
            "pid": None,
            "error": "No process found listening on port {}".format(port),
        }

    if ctx:
        await ctx.info(
            "Force-killing Revit PID {} on port {} (unsaved work WILL be lost)".format(
                pid, port
            )
        )

    try:
        if _is_windows():
            subprocess.check_call(
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            os.kill(int(pid), 9)  # SIGKILL
        return {"status": "success", "pid": int(pid)}
    except subprocess.CalledProcessError as e:
        return {"status": "error", "pid": int(pid), "error": str(e)}


def _primary_docs(docs: list) -> list:
    """Return only primary (non-linked) docs.

    Revit-link documents appear in Application.Documents but are owned by
    the primary doc that loaded them — closing / saving / syncing them
    individually is not how they're meant to be handled. Revit closes
    them automatically when the owning primary closes.
    """
    return [d for d in docs if not d.get("is_linked")]


def _modified_docs(docs: list) -> list:
    """Modified primary docs. Ignores linked docs entirely."""
    return [
        d for d in docs
        if d.get("is_modified") and not d.get("is_linked")
    ]


def _format_doc_list(docs: list) -> list:
    """Trim the doc list to the fields the caller actually needs."""
    return [
        {
            "title": d.get("title"),
            "path": d.get("path"),
            "is_active": d.get("is_active"),
            "is_modified": d.get("is_modified"),
            "is_workshared": d.get("is_workshared"),
            "is_detached": d.get("is_detached"),
            "is_family": d.get("is_family"),
            "is_linked": d.get("is_linked"),
        }
        for d in docs
    ]


def register_instance_tools(mcp, revit_get, revit_post):
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
        save_mode: str = "ask",
        force: bool = False,
    ) -> str:
        """Close a running Revit instance with configurable save/sync behavior.

        This tool never silently commits to disk. By default (save_mode="ask")
        it REFUSES to close if any document has unsaved changes — the caller
        must decide explicitly what to do with each dirty doc.

        Save vs Sync on workshared docs: these are DIFFERENT operations.
            Save writes to the local copy only.
            Sync pushes local changes to the central model (and saves).

        Args:
            instance: Revit version year (e.g. "2024"). Required when multiple
                Revits are running unless force=True.
            save_mode: One of:
                "ask"     - (default) refuse if any doc is modified; return
                            the dirty list so the caller can act explicitly.
                "save"    - Save every modified doc in place (local only for
                            workshared), then close each doc and exit Revit.
                "sync"    - Sync every modified workshared doc with central
                            (implies save), then close and exit. Refuses if
                            any non-workshared modified doc is present.
                "discard" - Close every doc without saving or syncing, then
                            exit Revit. Revit MAY still prompt on the active
                            doc via its native Close command; if that
                            happens, the tool falls back to 'force'.
                "force"   - Immediate hard kill (taskkill /F). Unsaved work
                            is lost. Same as force=True.
            force: Legacy alias for save_mode="force". If True, overrides
                save_mode and hard-kills the process. Keeps backward compat
                with the pre-graceful-close signature.
        """
        # Legacy force=True wins — it was the original escape hatch.
        if force:
            save_mode = "force"

        valid_modes = {"ask", "save", "sync", "discard", "force"}
        if save_mode not in valid_modes:
            return json.dumps(
                {
                    "status": "error",
                    "error": "Invalid save_mode '{}'. Use one of: {}".format(
                        save_mode, sorted(valid_modes)
                    ),
                },
                indent=2,
            )

        registry = await discover_instances()
        if not registry:
            return json.dumps(
                {"status": "error", "error": "No Revit instances running."},
                indent=2,
            )

        if not instance and save_mode != "force" and len(registry) > 1:
            # Summarise as version@port pairs so the caller can disambiguate.
            available = sorted(
                (
                    "{}@{}".format(info.get("version", "?"), info.get("port", "?"))
                    for info in registry.values()
                ),
                key=lambda s: s,
            )
            return json.dumps(
                {
                    "status": "error",
                    "error": (
                        "Multiple Revit instances running; refusing to auto-"
                        "select for a close operation. Pass instance=\"YEAR\" "
                        "or instance=\"PORT\" (e.g. \"48885\") to target one."
                    ),
                    "available": available,
                },
                indent=2,
            )

        try:
            port = await resolve_port(instance)
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)}, indent=2)

        # Resolve the version we're targeting for logging/response.
        # Registry is keyed by port; look up directly.
        target_version = None
        info = registry.get(str(port))
        if info:
            target_version = str(info.get("version"))

        # ---- force: straight to hard kill ----------------------------------
        if save_mode == "force":
            result = await _hard_terminate_by_port(port, ctx=ctx)
            if result.get("status") == "success" and target_version:
                await unregister_instance(str(port))
            return json.dumps(
                {
                    "status": result.get("status"),
                    "save_mode": "force",
                    "closed_version": target_version,
                    "port": port,
                    "pid": result.get("pid"),
                    "error": result.get("error"),
                    "warning": (
                        "Hard-killed via taskkill /F. Any unsaved work in "
                        "this Revit is lost."
                    ),
                },
                indent=2,
            )

        # ---- everything else needs the document list first ----------------
        list_resp = await revit_get(
            "/list_documents/", ctx, instance=instance
        )
        if not isinstance(list_resp, dict) or list_resp.get("error"):
            err_detail = (
                list_resp.get("error") if isinstance(list_resp, dict)
                else str(list_resp)
            )
            return json.dumps(
                {
                    "status": "error",
                    "error": (
                        "Could not list documents for Revit {}: {}".format(
                            target_version or instance, err_detail
                        )
                    ),
                },
                indent=2,
            )

        docs = list_resp.get("documents", [])
        dirty = _modified_docs(docs)

        # ---- ask: refuse if anything is dirty -----------------------------
        if save_mode == "ask":
            if dirty:
                return json.dumps(
                    {
                        "status": "refused",
                        "save_mode": "ask",
                        "reason": (
                            "One or more documents have unsaved changes. "
                            "Pass save_mode='save' (local only), 'sync' "
                            "(workshared → central), 'discard' (lose "
                            "changes), or 'force' (hard kill)."
                        ),
                        "closed_version": target_version,
                        "modified_documents": _format_doc_list(dirty),
                        "all_documents": _format_doc_list(docs),
                    },
                    indent=2,
                )
            # Nothing dirty — proceed with a clean close.
            return await _close_clean_and_terminate(
                ctx, revit_post, instance, port,
                target_version, docs, save_mode="ask",
            )

        # ---- save: Save() every modified doc ------------------------------
        if save_mode == "save":
            for d in dirty:
                try:
                    await revit_post(
                        "/save_document/",
                        {"file_path": None},
                        ctx,
                        instance=instance,
                        timeout=120.0,
                    )
                except Exception as e:
                    return json.dumps(
                        {
                            "status": "error",
                            "save_mode": "save",
                            "error": (
                                "Save failed for '{}': {}. No documents "
                                "closed; Revit still running."
                            ).format(d.get("title"), e),
                        },
                        indent=2,
                    )
            return await _close_clean_and_terminate(
                ctx, revit_post, instance, port,
                target_version, docs, save_mode="save",
            )

        # ---- sync: workshared only ---------------------------------------
        if save_mode == "sync":
            non_ws_dirty = [
                d for d in dirty if not d.get("is_workshared")
            ]
            if non_ws_dirty:
                return json.dumps(
                    {
                        "status": "refused",
                        "save_mode": "sync",
                        "reason": (
                            "Some modified documents are not workshared and "
                            "cannot be synced. Use save_mode='save' or "
                            "handle them explicitly first."
                        ),
                        "non_workshared_modified": _format_doc_list(
                            non_ws_dirty
                        ),
                        "all_documents": _format_doc_list(docs),
                    },
                    indent=2,
                )
            for d in dirty:
                try:
                    await revit_post(
                        "/sync_with_central/",
                        {
                            "comment": "Auto-sync before close_revit_instance",
                            "compact": False,
                            "relinquish_all": True,
                        },
                        ctx,
                        instance=instance,
                        timeout=300.0,
                    )
                except Exception as e:
                    return json.dumps(
                        {
                            "status": "error",
                            "save_mode": "sync",
                            "error": (
                                "Sync failed for '{}': {}. No documents "
                                "closed; Revit still running."
                            ).format(d.get("title"), e),
                        },
                        indent=2,
                    )
            return await _close_clean_and_terminate(
                ctx, revit_post, instance, port,
                target_version, docs, save_mode="sync",
            )

        # ---- discard: close without save, fall back to force on timeout ---
        if save_mode == "discard":
            # Try to close each PRIMARY doc without saving. PostCommand(Close)
            # on a dirty active doc MAY still show Revit's native prompt — in
            # that case graceful termination will time out and we hard-kill.
            # Linked docs are ignored; Revit closes them with the primary.
            for d in _primary_docs(docs):
                try:
                    await revit_post(
                        "/close_document/",
                        {"save": False},
                        ctx,
                        instance=instance,
                        timeout=30.0,
                    )
                except Exception:
                    continue

            graceful = await _graceful_terminate_by_port(
                port, ctx=ctx, timeout_seconds=15.0
            )
            if graceful.get("status") == "success":
                if target_version:
                    await unregister_instance(str(port))
                return json.dumps(
                    {
                        "status": "success",
                        "save_mode": "discard",
                        "closed_version": target_version,
                        "port": port,
                        "pid": graceful.get("pid"),
                        "elapsed": graceful.get("elapsed"),
                    },
                    indent=2,
                )

            # Graceful timed out — user asked for discard, so it's fair to
            # assume they want it gone. Fall back to hard kill.
            if ctx:
                await ctx.info(
                    "Graceful close timed out in discard mode; falling "
                    "back to taskkill /F per save_mode='discard' contract."
                )
            hard = await _hard_terminate_by_port(port, ctx=ctx)
            if hard.get("status") == "success" and target_version:
                await unregister_instance(str(port))
            return json.dumps(
                {
                    "status": hard.get("status"),
                    "save_mode": "discard",
                    "closed_version": target_version,
                    "port": port,
                    "pid": hard.get("pid"),
                    "fallback_used": "force",
                    "warning": (
                        "Graceful close timed out (Revit likely prompted "
                        "on a dirty active doc); hard-killed instead."
                    ),
                    "error": hard.get("error"),
                },
                indent=2,
            )

        # Shouldn't reach here.
        return json.dumps(
            {"status": "error", "error": "Unhandled save_mode"}, indent=2
        )


async def _close_clean_and_terminate(
    ctx, revit_post, instance, port, target_version, docs, save_mode
):
    """Close every doc (save=False, they're already clean) then graceful kill.

    Used by the save/sync/ask paths once we know nothing dirty remains.
    Only falls back to /F if graceful close times out AND save_mode was
    explicitly not 'ask' (we never force-kill on the default path).
    """
    # Close each primary doc. Linked docs are closed automatically by
    # their owning primary, so iterating them would be redundant.
    for d in _primary_docs(docs):
        try:
            await revit_post(
                "/close_document/",
                {"save": False},
                ctx,
                instance=instance,
                timeout=30.0,
            )
        except Exception:
            continue

    graceful = await _graceful_terminate_by_port(
        port, ctx=ctx, timeout_seconds=20.0
    )
    if graceful.get("status") == "success":
        if target_version:
            await unregister_instance(str(port))
        return json.dumps(
            {
                "status": "success",
                "save_mode": save_mode,
                "closed_version": target_version,
                "port": port,
                "pid": graceful.get("pid"),
                "elapsed": graceful.get("elapsed"),
            },
            indent=2,
        )

    # Graceful timed out. We do NOT auto-force on save/sync/ask — the
    # caller asked us to preserve their work, and a hung prompt could
    # indicate a save/sync issue that force-kill would destroy.
    return json.dumps(
        {
            "status": "still_running",
            "save_mode": save_mode,
            "closed_version": target_version,
            "port": port,
            "pid": graceful.get("pid"),
            "elapsed": graceful.get("elapsed"),
            "error": graceful.get("error"),
            "hint": (
                "Revit didn't exit within the timeout. Check the UI for "
                "a save/sync prompt. Retry or call again with "
                "save_mode='force' if you want to abandon whatever is "
                "blocking exit."
            ),
        },
        indent=2,
    )
