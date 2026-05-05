# -*- coding: utf-8 -*-
"""
Multi-instance registry for Revit MCP Server.

Discovers running Revit instances by probing pyRevit Routes ports
(48884 + auto-increment when multiple Revits are running), identifies
each by Revit version year, and caches the result so every subsequent
tool call can target a specific instance.

Public API:
    await discover_instances(force=False) -> Dict[port_str, InstanceInfo]
    await resolve_port(instance: str | None) -> int
    await invalidate()
    REVIT_HOST, REVIT_PORT_RANGE, PYREVIT_API_ROOT  (module constants)

The registry is keyed by pyRevit Routes port (as a string like "48884"), NOT
by Revit version year. This allows multiple Revits of the same version year
to coexist — e.g. three Revit 2024 instances on ports 48884/48885/48886.

Callers of resolve_port(instance) may pass:
    - a bare port number like "48885"  (direct targeting, always unambiguous)
    - a Revit version year like "2024" (picks the lowest-port instance of
      that year; back-compat for the single-instance-per-year case)
    - None                             (auto-selects: latest version year,
                                        lowest port of that year)
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
                    # Keyed by PORT (unique) so that multiple Revits of the
                    # same version year don't collapse into a single entry.
                    discovered[str(info["port"])] = info

            async with anyio.create_task_group() as tg:
                for port in REVIT_PORT_RANGE:
                    tg.start_soon(_one, port)

        _instances = discovered
        _initialised = True
        return dict(_instances)


def _looks_like_port(s: str) -> bool:
    """A caller-supplied instance string is a port if it's a pure integer
    that falls inside the scan range. Revit version years (2024/2025) are
    4-digit ints too, so we also check the range explicitly."""
    try:
        n = int(s)
    except (TypeError, ValueError):
        return False
    return REVIT_PORT_RANGE.start <= n < REVIT_PORT_RANGE.stop


def _describe_available(registry: Dict[str, InstanceInfo]) -> str:
    """Human-readable summary for error messages: version @ port pairs."""
    if not registry:
        return "(none)"
    entries = []
    for info in sorted(registry.values(), key=lambda i: int(i.get("port", 0))):
        entries.append(
            "{}@{}".format(info.get("version", "?"), info.get("port", "?"))
        )
    return ", ".join(entries)


async def resolve_port(instance: Optional[str] = None) -> int:
    """Return the pyRevit Routes port for the target Revit instance.

    If instance is None:
      - Exactly one Revit running: use it.
      - Multiple Revits running: use the latest version year (e.g. 2025 > 2024);
        if the latest year has multiple instances, pick the lowest port.
      - Zero Revits: RuntimeError.

    If instance is a PORT NUMBER string like "48885":
      - Look it up directly in the port-keyed registry.
      - Rescan if unknown; raise RuntimeError if still not found.

    If instance is a VERSION YEAR string like "2024":
      - Find all instances matching that version; pick the lowest port.
        (For back-compat with the single-instance-per-year case.)
      - When multiple same-year instances exist, callers that need to
        target a specific one must pass a port number instead.
      - Rescan if unknown; raise RuntimeError if still not found.
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

        # Path 1: direct port targeting
        if _looks_like_port(key):
            if key not in registry:
                registry = await discover_instances(force=True)
            if key not in registry:
                raise RuntimeError(
                    "No Revit instance listening on port {}. Available: {}".format(
                        key, _describe_available(registry)
                    )
                )
            return int(registry[key]["port"])

        # Path 2: version year — find all matching, pick lowest port.
        matches = [
            info for info in registry.values()
            if str(info.get("version")) == key
        ]
        if not matches:
            registry = await discover_instances(force=True)
            matches = [
                info for info in registry.values()
                if str(info.get("version")) == key
            ]
        if not matches:
            raise RuntimeError(
                "Revit {} is not running. Available: {}".format(
                    key, _describe_available(registry)
                )
            )
        matches.sort(key=lambda i: int(i.get("port", 0)))
        return int(matches[0]["port"])

    # No instance specified.
    if len(registry) == 1:
        return int(next(iter(registry.values()))["port"])

    # Multiple instances — prefer the latest numeric version, then lowest port.
    def _sort_key(info: InstanceInfo):
        v = str(info.get("version", ""))
        try:
            vnum = int(v)
        except ValueError:
            vnum = -1
        # Negate vnum so max() picks the highest; port ascending for tiebreak.
        return (-vnum, int(info.get("port", 999999)))

    chosen = min(registry.values(), key=_sort_key)
    return int(chosen["port"])


async def invalidate() -> None:
    """Force the next discover_instances() call to re-scan."""
    global _initialised
    async with _lock:
        _initialised = False


async def register_instance(version: str, port: int, document_title: Optional[str] = None) -> None:
    """Manually insert a newly-launched instance into the registry.

    Useful right after launch_revit so subsequent calls don't pay the
    rediscovery cost. Keyed by port so multiple same-year instances coexist.
    """
    async with _lock:
        _instances[str(int(port))] = {
            "version": str(version),
            "port": int(port),
            "document_title": document_title,
            "document_active": document_title is not None,
        }


async def unregister_instance(version_or_port: str) -> None:
    """Remove an instance from the registry.

    Accepts either a port string (preferred, unambiguous) or a version year.
    When multiple instances share a version year, only the lowest-port one
    is removed — callers that need precision should pass a port.
    """
    async with _lock:
        key = str(version_or_port)
        # Direct port hit first
        if key in _instances:
            _instances.pop(key, None)
            return
        # Fall back to version-year lookup (lowest port wins)
        matches = sorted(
            (p for p, info in _instances.items() if str(info.get("version")) == key),
            key=lambda p: int(p),
        )
        if matches:
            _instances.pop(matches[0], None)
