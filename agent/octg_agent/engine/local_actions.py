"""Actions that run inside the agent (no browser round trip): state, events, task label and authorized-folder file operations."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..files.authorization import AuthorizationError
from .protocol import AgentError, ErrorCode

if TYPE_CHECKING:  # pragma: no cover
    from .dispatcher import Dispatcher


def _auth(e: AuthorizationError) -> AgentError:
    return AgentError(e.code, e.message, e.details)


async def get_agent_state(d: "Dispatcher", p: dict) -> dict:
    return {"result": {"state": d.state.summary(), "authorized_folders": len(d.folders.root_paths()), "max_upload_bytes": d.settings.max_upload_bytes}}


async def get_events(d: "Dispatcher", p: dict) -> dict:
    ev = d.state.events_since(int(p.get("since", 0)), int(p.get("limit", 100)))
    return {"result": {"events": ev, "latest_seq": d.state.summary()["event_seq"]}}


async def set_task(d: "Dispatcher", p: dict) -> dict:
    d.state.task = p.get("task") or None
    await d.push_status()
    return {"result": {"task": d.state.task}}


async def fs_list_roots(d: "Dispatcher", p: dict) -> dict:
    return {"result": {"roots": d.folders.roots()}}


async def fs_find(d: "Dispatcher", p: dict) -> dict:
    try:
        files = d.folders.find(p["pattern"], p.get("root"), int(p.get("max_results", 50)))
    except AuthorizationError as e:
        raise _auth(e) from e
    if not d.folders.root_paths():
        raise AgentError(ErrorCode.FILE_NOT_AUTHORIZED, "No folders are authorized yet. Run: python -m octg_agent fs allow <folder>")
    return {"result": {"files": files, "count": len(files)}}


async def fs_save_download(d: "Dispatcher", p: dict) -> dict:
    dl = d.state.downloads.get(p["download_id"])
    if not dl or dl.get("state") != "complete" or not dl.get("filename"):
        listing = await d.call_extension("list_downloads", {"limit": 100})
        for item in (listing.get("result") or {}).get("downloads", []):
            if item["download_id"] == p["download_id"]:
                dl = item
                break
    if not dl or not dl.get("filename"):
        raise AgentError("DOWNLOAD_NOT_FOUND", f"Chrome has no download with id {p['download_id']}.")
    if dl.get("state") != "complete":
        raise AgentError(ErrorCode.POSTCONDITION_FAILED, f"Download {p['download_id']} is {dl.get('state')}, not complete.")
    src = dl["filename"]
    try:
        saved = d.folders.save_into(p["dest_dir"], p.get("filename") or Path(src).name, src, bool(p.get("overwrite")))
    except AuthorizationError as e:
        raise _auth(e) from e
    return {"result": {**saved, "source": src}}


LOCAL: dict[str, Any] = {
    "get_agent_state": get_agent_state,
    "get_events": get_events,
    "set_task": set_task,
    "fs_list_roots": fs_list_roots,
    "fs_find": fs_find,
    "fs_save_download": fs_save_download,
}
