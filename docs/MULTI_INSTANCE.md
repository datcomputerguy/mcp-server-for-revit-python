# Multi-Instance Support

As of this version, the MCP server can drive **multiple simultaneous Revit
instances** running different versions (e.g. Revit 2024 + Revit 2025 side by
side). Every tool accepts an optional `instance` parameter to choose which
Revit to talk to, and two new tools (`list_revit_instances`,
`close_revit_instance`) let the AI discover and manage them.

## How it works

pyRevit Routes binds to port **48884** on the first Revit to start. When a
second Revit launches with pyRevit loaded, Routes auto-increments to
**48885**, a third to **48886**, and so on. Previously this server was
hardcoded to 48884, so only the first-launched Revit was reachable.

The new `instance_registry` module scans ports **48884-48894**, hits each
responding port's `/revit_mcp/status/` endpoint, and asks each Revit for
its version number via a new `/revit_mcp/instance_info/` route. The
registry caches the mapping `{version_year: port}` so every subsequent tool
call resolves to the right Revit without re-scanning.

## Tool changes

Every existing tool gained an optional `instance` parameter (a Revit version
year string like `"2024"` or `"2025"`):

- `get_revit_status(instance=None)`
- `get_revit_model_info(instance=None)`
- `execute_revit_code(code, description, instance=None)`
- `open_document(file_path, ..., instance=None)`
- `close_document(save, instance=None)`
- `save_document(file_path, instance=None)`
- `sync_with_central(..., instance=None)`
- `list_revit_views(instance=None)`, `get_revit_view(view_name, instance=None)`,
  `get_current_view_info(instance=None)`, `get_current_view_elements(instance=None)`
- `list_levels(instance=None)`
- `place_family(..., instance=None)`, `list_families(..., instance=None)`,
  `list_family_categories(instance=None)`
- `color_splash(..., instance=None)`, `clear_colors(..., instance=None)`,
  `list_category_parameters(..., instance=None)`

### Default resolution

When `instance` is omitted:
- **Zero Revits running** → error ("no active Revit instances found").
- **One Revit running** → that one is used.
- **Multiple Revits running** → the **newest version year** wins (e.g.
  Revit 2025 is preferred over Revit 2024).

The auto-pick rule matches what most users expect but means you should pass
`instance="2024"` explicitly when driving an older Revit while a newer one
is also running.

## New tools

### `list_revit_instances(refresh=False)`
Returns a JSON listing of all detected instances with their Revit version,
port, and current document title. Pass `refresh=True` to force a rescan
after launching or closing an instance externally.

```json
{
  "status": "success",
  "count": 2,
  "default_version_if_unspecified": "2025",
  "instances": [
    {"version": "2025", "port": 48884, "document_title": "MyModel_2025", "document_active": true},
    {"version": "2024", "port": 48885, "document_title": "MyModel_2024", "document_active": true}
  ]
}
```

### `close_revit_instance(instance=None, force=False)`
Terminates the Revit process bound to the given instance's port. If
multiple Revits are running and no `instance` is passed, the call is
refused unless `force=True` (safety guard against accidentally killing
the wrong one). Revit will still prompt to save any unsaved work — this
tool does not auto-dismiss that dialog.

### `launch_revit(...)` enhancements
- Now registers the newly-launched Revit in the instance registry on
  startup so subsequent tool calls targeting that version just work.
- Default `timeout` raised to 600 seconds (10 min) to cover cloud model
  downloads.
- New `wait_for_document` option (auto-enabled when `file_path` is given)
  makes the tool wait until the document has actually finished loading,
  not just until Revit itself is up. Useful for cloud-based workshared
  models that take minutes to download on first open.

## New endpoint (pyRevit side)

`GET /revit_mcp/instance_info/` — lightweight read-only endpoint returning
Revit version number, build, application username, and a list of open
document titles. Added so the registry can identify a Revit instance
without having to execute code (which required an active document on
some pyRevit builds).

Older `revit_mcp` packages without this route still work — the registry
falls back to running `print(__revit__.Application.VersionNumber)` via
`/execute_code/`.

## Migration notes for existing clients

- All changes are **backwards-compatible**. Tool calls without an
  `instance` parameter behave the same as before when only one Revit is
  running, which is the typical case.
- If you are running more than one Revit simultaneously and previously
  the server happened to talk to 48884, the new default-resolution rule
  (latest version wins) may change which instance it targets. Pass
  `instance` explicitly to remove the ambiguity.
- If you want to revert to single-instance behavior, the registry
  gracefully degrades when only one pyRevit Routes port is responding —
  it will always pick that one.

## Not covered (yet)

- Automatic dismissal of Revit's blocking dialogs during model load
  (missing worksets, warnings, "convert to central model" prompts).
  Users currently need to click through those manually or via windows-mcp.
- Assigning a deterministic port to a specific Revit version. pyRevit
  Routes allocates ports in launch order, so the same Revit version
  may land on a different port between sessions.
