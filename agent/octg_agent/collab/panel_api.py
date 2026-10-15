"""Commands the Chrome side panel sends for the two features that used to live only in the Hub: Ask the agent (ask_*) and the Inbox of AI requests (inbox_*).

The panel is the owner's own interface (it is paired with the agent by the owner's token), so these commands run the same code as the Hub pages. What they can NOT do: approve or deny anything (that needs the PIN
and stays in the Hub), change a business record, or run anything but a read-only Google search. The one browser-reading command, `inbox_attach`, runs only when the owner presses the attach button, on the
tab the owner is looking at, and shows exactly what was attached before the reply is sent.
"""
from __future__ import annotations

import time
import uuid

from ..business.store import StoreError
from ..sensitive import scrub_text
from .asks import AskError, AskService
from .store import CollabError

DRAFT_TTL_S = 3600
PAGE_CHARS = 12000


class PanelError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _brief(r: dict, names: dict) -> dict:
    return {**{k: v for k, v in r.items() if k not in ("attachments", "created_by")}, "attachments": [{k: x.get(k) for k in ("kind", "url", "title", "chars")} for x in r["attachments"]],
            "client_name": names.get(r["client_id"] or "", "")}


class PanelApi:
    def __init__(self, rt):
        self.rt = rt
        self.drafts: dict[str, dict] = {}

    @property
    def svc(self):
        return self.rt.collab

    SLOW = {"inbox_attach"}     # commands that wait for the extension to read the tab

    def handles(self, cmd: str) -> bool:
        return cmd.startswith(("ask_", "inbox_"))

    async def handle(self, cmd: str, msg: dict) -> dict:
        try:
            return {"ok": True, "panel": await self._run(cmd, msg)}
        except (AskError, PanelError) as e:
            return {"ok": False, "error": e.message, "code": e.code}
        except CollabError as e:
            return {"ok": False, "error": e.message, "code": e.code}
        except StoreError as e:
            return {"ok": False, "error": e.message, "code": e.code}

    async def _run(self, cmd: str, m: dict) -> dict:
        svc = self.svc
        asks = AskService(svc)
        if cmd == "ask_clients":
            return asks.clients()
        if cmd == "ask_send":
            return asks.send(text=m.get("text"), client_id=m.get("client_id"), queries=m.get("queries") if isinstance(m.get("queries"), list) else None, location=m.get("location") or "")
        if cmd == "ask_list":
            return {"asks": asks.list(limit=12)}
        if cmd == "ask_cancel":
            asks.cancel(str(m.get("ask_id") or ""))
            return {"cancelling": True}
        names = {c["client_id"]: c["name"] for c in svc.admin().store.list_clients(include_archived=True)}
        if cmd == "inbox_list":
            return {"requests": [_brief(r, names) for r in svc.inbox.list(limit=30)], "open": svc.inbox.open_count()}
        if cmd == "inbox_reply":
            oid = str(m.get("or_id") or "")
            atts = []
            for aid in m.get("attachments") or []:
                d = self.drafts.get(str(aid))
                if not d or d["or_id"] != oid:
                    raise PanelError("ATTACHMENT_GONE", "that attachment is no longer available: attach the tab again")
                atts += d["items"]
            r = svc.inbox.answer(oid, text=str(m.get("text") or ""), choice=str(m.get("choice") or ""), attachments=atts, via="panel")
            for aid in list(self.drafts):
                if self.drafts[aid]["or_id"] == oid:
                    del self.drafts[aid]
            svc.notify_inbox()
            return {"request": _brief(r, names), "open": svc.inbox.open_count()}
        if cmd == "inbox_dismiss":
            svc.inbox.dismiss(str(m.get("or_id") or ""), via="panel")
            svc.notify_inbox()
            return {"dismissed": True, "open": svc.inbox.open_count()}
        if cmd == "inbox_attach":
            return await self._attach(m)
        raise PanelError("UNKNOWN_COMMAND", f"the agent does not know the command {cmd}")

    # ------------------------------------------------------------------ attach what the owner is looking at (owner pressed the button)
    async def _attach(self, m: dict) -> dict:
        svc, rt = self.svc, self.rt
        oid, tab = str(m.get("or_id") or ""), m.get("tab_id")
        req = svc.inbox.get(oid)
        if req["status"] != "open":
            raise PanelError("NOT_OPEN", "that request is already closed")
        if not isinstance(tab, int) or isinstance(tab, bool):
            raise PanelError("NO_TAB", "there is no tab to attach")
        if rt.session is None:
            raise PanelError("NOT_CONNECTED", "Chrome is not connected to the agent")
        now = time.time()
        for k in [k for k, d in self.drafts.items() if now - d["at"] > DRAFT_TTL_S]:
            del self.drafts[k]
        d = rt.dispatcher
        st = await d.execute("get_page_state", {"tab_id": tab}, source="panel")
        if not st.get("success"):
            raise PanelError((st.get("error") or {}).get("code") or "CANNOT_READ", "This tab cannot be read by the extension (Chrome's own pages such as chrome:// block it). Open the page itself and try again.")
        page = (st.get("result") or {}).get("page") or st.get("result") or {}
        url, title = str(page.get("url") or "")[:500], str(page.get("title") or "")[:200]
        rd = await d.execute("read_page", {"tab_id": tab, "scope": "page", "max_chars": PAGE_CHARS}, source="panel")
        if not rd.get("success"):
            raise PanelError((rd.get("error") or {}).get("code") or "CANNOT_READ", "The text of this tab could not be read.")
        text = scrub_text((rd.get("result") or {}).get("text") or "")[:PAGE_CHARS]
        items = []
        ev = svc.store.add_evidence(request_id=oid, task_id=req["task_id"], client_id=req["client_id"], kind="owner_page_text", content=text, source_url=url, summary="page text the owner attached", ext="txt")
        items.append({"kind": "page_text", "evidence_id": ev["evidence_id"], "url": url, "title": title, "chars": len(text)})
        sh = await d.execute("screenshot", {"tab_id": tab, "format": "jpeg", "quality": 70}, source="panel")
        path = (sh.get("result") or {}).get("path") if sh.get("success") else None
        if path:
            from pathlib import Path
            try:
                e2 = svc.store.add_evidence(request_id=oid, task_id=req["task_id"], client_id=req["client_id"], kind="owner_screenshot", content=Path(path).read_bytes(), source_url=url, summary="screenshot the owner attached", ext="jpg")
                items.append({"kind": "screenshot", "evidence_id": e2["evidence_id"], "url": url, "title": title})
            except OSError:
                pass
        att = "att_" + uuid.uuid4().hex[:10]
        self.drafts[att] = {"or_id": oid, "items": items, "at": now}
        return {"attachment": {"att_id": att, "url": url, "title": title, "chars": len(text), "screenshot": any(i["kind"] == "screenshot" for i in items)}}
