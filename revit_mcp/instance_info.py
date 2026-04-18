# -*- coding: UTF-8 -*-
"""
Instance Info Module for Revit MCP

Exposes a lightweight /instance_info/ endpoint so the MCP server (or any other
HTTP client) can identify which Revit version is answering on a given pyRevit
Routes port without having to fall back to code execution. This enables
multi-instance support: scan ports 48884+ and map each one to a version.
"""

import logging

from pyrevit import routes

logger = logging.getLogger(__name__)


def register_instance_info_routes(api):
    """Register /instance_info/ on the revit_mcp API."""

    @api.route("/instance_info/", methods=["GET"])
    def instance_info():
        """Return a small JSON payload identifying this Revit instance.

        Safe to call with no active document. Never modifies the model.
        """
        try:
            # __revit__ is injected by Revit into pyRevit's IronPython runtime.
            app = __revit__.Application  # noqa: F821
            version_number = getattr(app, "VersionNumber", None)
            version_build = getattr(app, "VersionBuild", None)
            version_name = getattr(app, "VersionName", None)
            username = getattr(app, "Username", None)

            # Count open documents and pick a friendly title if there is one.
            doc_titles = []
            try:
                for d in app.Documents:
                    if d and not d.IsFamilyDocument:
                        doc_titles.append(d.Title or "Untitled")
            except Exception:  # noqa: BLE001
                pass

            return routes.make_response(
                data={
                    "api_name": "revit_mcp",
                    "status": "ok",
                    "version_number": version_number,  # e.g. "2025"
                    "version_build": version_build,
                    "version_name": version_name,
                    "username": username,
                    "open_document_count": len(doc_titles),
                    "open_document_titles": doc_titles,
                }
            )
        except Exception as e:  # noqa: BLE001
            logger.error("instance_info failed: {}".format(e))
            return routes.make_response(
                data={
                    "api_name": "revit_mcp",
                    "status": "error",
                    "error": str(e),
                },
                status=500,
            )

    logger.info("Instance info routes registered successfully")
