# Multi-instance support (Revit 2024 + 2025 + ... running side by side)

## What this adds

Right now the MCP server hardcodes `REVIT_PORT = 48884`, which is the port
pyRevit Routes binds to in the first Revit to start. If a user has two or
three Revits running at once (very common for consultants bouncing between
versioned projects), only the first one is reachable — subsequent Revits
auto-increment to 48885, 48886, etc. and the MCP server can't see them.

This PR makes every tool multi-instance-aware:

- New `instance_registry` module scans ports 48884-48894 on demand, detects
  running Revits via their `/status/` endpoint, and identifies each by
  version year (`"2024"`, `"2025"`, ...).
- Every tool gains an optional `instance: str = None` parameter. When the
  AI passes `instance="2024"`, the call is routed to Revit 2024's port;
  otherwise the registry's default-resolution rule picks an instance
  (single-running: use it; multi-running: newest version wins).
- Two new tools:
  - `list_revit_instances(refresh=False)` — enumerate active instances.
  - `close_revit_instance(instance=None, force=False)` — terminate a
    specific Revit process by port, with a safety guard when multiple
    are running.
- `launch_revit()` gets a `wait_for_document` option (auto-enabled when
  `file_path` is given) and a default `timeout` of 600 seconds so
  cloud-based workshared models have time to download on first open.
  After launch, the new instance is automatically added to the registry.

## New pyRevit-side endpoint

`GET /revit_mcp/instance_info/` — tiny read-only endpoint returning the
Revit version number, build, username, and open-document titles. Used by
the registry to identify each port's Revit version cleanly. Older
`revit_mcp` packages that don't have this route still work: the registry
falls back to `/execute_code/` to print `__revit__.Application.VersionNumber`.

## Backwards compatibility

- All `instance` parameters default to `None` and preserve the prior
  behavior when only one Revit is running.
- If a single instance is running on a non-default port (e.g. user started
  Revit 2024 first, it grabbed 48884, then they closed and reopened
  something), the registry discovers it regardless.
- Old clients that call tools without `instance` continue to work.

## Why this matters

The pyRevit-based MCP has a unique advantage over the C# alternative:
IronPython code can call into any pyRevit extension's `lib/` modules, fire
pyRevit buttons via `sessionmgr.execute_command()`, and read structured
state from the running Revit. But all of that is gated on reaching the
right Revit instance. For any multi-version workflow — consultants working
across 2024 legacy projects and 2025 new projects, regression testers
comparing behavior across Revit builds, shops with mixed-version teams —
single-instance was a hard wall.

## Tested against

- Two simultaneous Revits (2024 + 2025) loading two different cloud
  (workshared) models. Both instances enumerated correctly, version-year
  lookup works, `execute_revit_code` targets the right one.
- Launch a third Revit version via `launch_revit` and confirm it auto-
  registers.
- Close an instance via `close_revit_instance` and confirm it drops out
  of the registry on the next scan.

## Files changed

- `instance_registry.py` — new, port scan + version cache.
- `main.py` — use registry for routing; `revit_get`/`revit_post` take
  `instance`.
- `tools/instance_tools.py` — new, adds `list_revit_instances` and
  `close_revit_instance`.
- `tools/__init__.py` — register the new module.
- `tools/launch_tools.py` — post-launch registration, wait-for-document
  option, longer default timeout.
- All other `tools/*.py` — thread `instance` through to the underlying
  HTTP wrappers.
- `revit_mcp/instance_info.py` — new, pyRevit-side `/instance_info/`
  route.
- `startup.py` — register the new route.
- `docs/MULTI_INSTANCE.md` — user-facing docs.

## Not in this PR (possible follow-ups)

- Automatic dismissal of Revit's blocking model-load dialogs (missing
  worksets, warnings, conversion prompts). Currently the user still has
  to click through those manually.
- Deterministic port-to-version binding. pyRevit Routes allocates ports
  in launch order, so the same Revit version may land on a different
  port across sessions. The registry handles this, but scripts that
  hard-code ports will still break.
- A `switch_default_instance(version)` tool that persists a preferred
  instance across the registry's lifetime so the AI doesn't have to
  pass `instance=...` on every call in a multi-Revit session.
