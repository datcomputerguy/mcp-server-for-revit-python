# -*- coding: utf-8 -*-
"""
Multi-instance registry for Revit MCP Server.

Discovers running Revit instances by probing pyRevit Routes ports
(48884 + auto-increment when multiple Revits are running), identifies
each by Revit version year, and caches the result so every subsequent
tool call can target a specific instance.

Public API:
    await discover_instances(force=False) -> Dict[version_year, InstanceInfo]
    await resolve_port(instance: str | None) -> int
    await invalidate()
    REVIT_HOST, REVIT_PORT_RANGE, PYREVIT_API_ROOT  (module constants)
"""

import anyio
import httpx
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REVIT_HOST = "localhost"
# pyRevit Routes binds to 48884; a second Revit auto-increments to 48885,
# a third to 48886, and so on. We probe a short range to find them all.
REVIT_PORT_RANGE = range(48884, 48895)
PYREVIT_API_ROOT = "revit_mcp"
PROBE_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------
# InstanceInfo is a plain dict for easy JSON serialisation:
#   {
#     "version": "2025",           # Revit version year as string
#     "port": 48884,               # pyRevit Routes port
#     "document_title": "MyModel", # None when no doc is active
#     "document_active": True,     # health == "healthy"
#   }
InstanceInfo = Dict[str, object]


# ---------------------------------------------------------------------------
# Registry state
# ---------------------------------------------------------------------------
_instances: Dict[str, InstanceInfo] = {}
_lock = anyio.Lock()
_initialised = False


def _base_url(port: int) -> str:
    return f"http://{REVIT_HOST}:{port}/{PYREVIT_API_ROOT}"


async def _probe_one(client: httpx.AsyncClient, port: int) -> Optional[InstanceInfo]:
    """Probe a single port. Returns None if nothing is there."""
    status_url = f"{_base_url(port)}/status/"
    try:
        resp = await client.get(status_url, timeout=PROBE_TIMEOUT)
    except httpx.RequestError:
        return None

    # pyRevit Routes returns 200 when healthy (doc active) and 503 when
    # no document is loaded yet. Both mean "server is up".
    if resp.status_code not in (200, 503):
        return None

    try:
        status_data = resp.json()
    except ValueError:
        return None

    # Confirm it's actually the revit_mcp API, not some other service
    if status_data.get("api_name") != "revit_mcp":
        return None

    version = await _probe_version(client, port)
    if not version:
        # Routes responded but we couldn't identify the Revit version —
        # record it anyway under "unknown" so the user isn't completely blind.
        version = "unknown"

    return {
        "version": version,
        "port": port,
        "document_title": status_data.get("document_title"),
        "document_active": resp.status_code == 200,
    }


async def _probe_version(client: httpx.AsyncClient, port: int) -> Optional[str]:
    """Ask the Revit instance for its version via a small execute_code call."""
    # Prefer the dedicated /instance_info/ route if available (added alongside
    # this registry). Fall back to execute_code for older revit_mcp packages.
    info_url = f"{_base_url(port)}/instance_info/"
    try:
        resp = await client.get(info_url, timeout=PROBE_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            v = data.get("version_number") or data.get("version")
            if v:
                return str(v).strip()
    except httpx.RequestError:
        pass

    # Fallback: run a tiny snippet via the existing execute_code route.
    exec_url = f"{_base_url(port)}/execute_code/"
    payload = {
        "code": "print(__revit__.Application.VersionNumber)",
        "description": "instance version probe",
    }
    try:
        resp = await client.post(
            exec_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=PROBE_TIMEOUT,
        )
    except httpx.RequestError:
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except ValueError:
        return None

    if data.get("status") != "success":
        return None
    return (data.get("output") or "").strip() or None


async def discover_instances(force: bool = False) -> Dict[str, InstanceInfo]:
    """Scan the pyRevit Routes port range and refresh the registry.

    Cached after the first call; pass force=True to re-scan.
    """
    global _instances, _initialised
    async with _lock:
        if not force and _initialised and _instances:
            return dict(_instances)

        discovered: Dict[str, InstanceInfo] = {}
        async with httpx.AsyncClient() as client:
            # Probe concurrently — each port takes up to PROBE_TIMEOUT s on miss.
            async def _one(p: int):
                info = await _probe_one(client, p)
                if info:
                    # Duplicate version keys (unlikely but possible if two
                    # Revits report the same VersionNumber somehow) —
                    # keep the lower-numbered port.
                    v = info["version"]
                    if v not in discovered or info["port"] < discovered[v]["port"]:
                        discovered[v] = info

            async with anyio.create_task_group() as tg:
                for port in REVIT_PORT_RANGE:
                    tg.start_soon(_one, port)

        _instances = discovered
        _initialised = True
        return dict(_instances)


async def resolve_port(instance: Optional[str] = None) -> int:
    """Return the pyRevit Routes port for the target Revit instance.

    If instance is None:
      - Exactly one Revit running: use it.
      - Multiple Revits running: use the latest version year (e.g. 2025 > 2024).
      - Zero Revits: RuntimeError.

    If instance is a version year string like "2024":
      - Rescan if unknown to pick up newly-launched Revits.
      - Raise RuntimeError with available versions if still not found.
    """
    registry = await discover_instances()
    if not registry:
        raise RuntimeError(
            "No active Revit instances found (scanned ports "
            f"{REVIT_PORT_RANGE.start}-{REVIT_PORT_RANGE.stop - 1}). "
            "Ensure Revit is running with pyRevit Routes enabled."
        )

    if instance:
        key = str(instance)
        if key not in registry:
            registry = await discover_instances(force=True)
        if key not in registry:
            available = ", ".join(sorted(registry.keys())) or "(none)"
            raise RuntimeError(
                f"Revit {key} is not running. Available: {available}"
            )
        return int(registry[key]["port"])

    if len(registry) == 1:
        return int(next(iter(registry.values()))["port"])

    # Multiple instances — prefer the latest numeric version.
    def _sort_key(v: str):
        try:
            return (0, int(v))
        except ValueError:
            return (1, v)

    latest = max(registry.keys(), key=_sort_key)
    return int(registry[latest]["port"])


async def invalidate() -> None:
    """Force the next discover_instances() call to re-scan."""
    global _initialised
    async with _lock:
        _initialised = False


async def register_instance(version: str, port: int, document_title: Optional[str] = None) -> None:
    """Manually insert a newly-launched instance into the registry.

    Useful right after launch_revit so subsequent calls don't pay the
    rediscovery cost.
    """
    async with _lock:
        _instances[str(version)] = {
            "version": str(version),
            "port": int(port),
            "document_title": document_title,
            "document_active": document_title is not None,
        }


async def unregister_instance(version: str) -> None:
    """Remove an instance (e.g. after closing that Revit)."""
    async with _lock:
        _instances.pop(str(version), None)
