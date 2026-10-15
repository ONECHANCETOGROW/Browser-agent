"""The connector registry: credentials status, live verification, and the OWNER-controlled mapping of provider properties to clients.

Mapping rules (these are what keep one client's data from reaching another):
  * only the owner maps (Hub with PIN, or the terminal): the collaborator can read a suggestion list limited to properties whose domain matches the client's own website, never the full list;
  * a property can be mapped to ONE client only (UNIQUE source+property);
  * a property whose domain differs from every website the client has needs an explicit `allow_mismatch` from the owner;
  * before EVERY use the credential's current view of its properties is checked (cached a few minutes): a property that disappeared or lost its permission raises UNAUTHORIZED_PROPERTY.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import LABEL, SOURCES, ConnectorError, domain_of, scrub
from .bing import HOW as BING_HOW, BingWebmaster
from .gsc import HOW as GSC_HOW, QUERYABLE, GscClient
from .http import Http

DDL = """
CREATE TABLE IF NOT EXISTS connector_mappings (
  map_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, source TEXT NOT NULL, property TEXT NOT NULL, permission TEXT, domain TEXT,
  mapped_at REAL, mapped_by TEXT, last_ok_at REAL, last_error TEXT, UNIQUE (source, property)
);
CREATE INDEX IF NOT EXISTS connector_map_client ON connector_mappings(client_id, source);
CREATE TABLE IF NOT EXISTS connector_state (
  source TEXT PRIMARY KEY, verified_at REAL, ok INTEGER, code TEXT, message TEXT, sites INTEGER, kind TEXT
);
"""
SITES_TTL_S = 300.0


@dataclass
class Resolved:
    source: str
    property: str
    permission: str
    map_id: str


class ConnectorStore:
    def __init__(self, store):
        self.store = store
        with store.conn() as c:
            c.executescript(DDL)

    def add(self, client_id: str, source: str, prop: str, permission: str, mapped_by: str) -> dict:
        from ..collab.store import new_id
        mid = new_id("cm")
        with self.store.conn() as c:
            c.execute("INSERT INTO connector_mappings (map_id,client_id,source,property,permission,domain,mapped_at,mapped_by) VALUES (?,?,?,?,?,?,?,?)",
                      (mid, client_id, source, prop, permission, domain_of(prop), time.time(), mapped_by))
        return self.get(mid)

    def get(self, map_id: str) -> dict:
        with self.store.conn() as c:
            r = c.execute("SELECT * FROM connector_mappings WHERE map_id=?", (map_id,)).fetchone()
        if not r:
            raise ConnectorError("NO_SUCH_MAPPING", "no such mapping")
        return dict(r)

    def mappings(self, *, client_id: str | None = None, source: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM connector_mappings WHERE 1=1", []
        for col, v in (("client_id", client_id), ("source", source)):
            if v:
                q += f" AND {col}=?"
                a.append(v)
        with self.store.conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY mapped_at", a).fetchall()]

    def owner_of(self, source: str, prop: str) -> str | None:
        with self.store.conn() as c:
            r = c.execute("SELECT client_id FROM connector_mappings WHERE source=? AND property=?", (source, prop)).fetchone()
        return r["client_id"] if r else None

    def remove(self, map_id: str) -> None:
        with self.store.conn() as c:
            c.execute("DELETE FROM connector_mappings WHERE map_id=?", (map_id,))

    def touch(self, map_id: str, error: str = "") -> None:
        with self.store.conn() as c:
            c.execute("UPDATE connector_mappings SET last_ok_at=CASE WHEN ?='' THEN ? ELSE last_ok_at END, last_error=? WHERE map_id=?", (error, time.time(), error, map_id))

    def set_state(self, source: str, ok: bool, code: str, message: str, sites: int, kind: str | None) -> None:
        with self.store.conn() as c:
            c.execute("INSERT INTO connector_state (source,verified_at,ok,code,message,sites,kind) VALUES (?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
                      "verified_at=excluded.verified_at, ok=excluded.ok, code=excluded.code, message=excluded.message, sites=excluded.sites, kind=excluded.kind",
                      (source, time.time(), int(ok), code, scrub(message)[:300], sites, kind))

    def state(self, source: str) -> dict | None:
        with self.store.conn() as c:
            r = c.execute("SELECT * FROM connector_state WHERE source=?", (source,)).fetchone()
        return dict(r) if r else None


class ConnectorHub:
    def __init__(self, folder: Path | str, store: ConnectorStore, *, http: Http | None = None, clock=time.time):
        self.folder, self.db, self.clock = Path(folder), store, clock
        self.http = http or Http()
        self._sites: dict[str, tuple[float, list[dict]]] = {}
        self._clients: dict[str, object] = {}

    # ------------------------------------------------------------------ clients (rebuilt when the transport is swapped, e.g. by tests)
    def client(self, source: str):
        if source not in SOURCES:
            raise ConnectorError("UNKNOWN_SOURCE", f"unknown data source {source!r}")
        c = self._clients.get(source)
        if c is None or c.http is not self.http:
            c = (GscClient if source == "google_search_console" else BingWebmaster)(self.folder, self.http, self.clock)
            self._clients[source] = c
            self._sites.pop(source, None)
        return c

    def how(self, source: str) -> str:
        return GSC_HOW if source == "google_search_console" else BING_HOW

    # ------------------------------------------------------------------ status
    def status(self) -> list[dict]:
        rows = []
        for src in SOURCES:
            cfg = self.client(src).configured()
            st = self.db.state(src)
            maps = self.db.mappings(source=src)
            connected = bool(cfg["configured"] and st and st["ok"])
            if connected:
                missing = ""
            elif not cfg["configured"]:
                missing = cfg["problem"]
            elif not st:
                missing = "credentials are present but have not been verified yet (run connectors.verify)"
            else:
                missing = f"the last verification failed: {st['code']}: {st['message']}"
            rows.append({"source": src, "label": LABEL[src], "connected": connected, "credentials_present": bool(cfg["configured"]), "credential_kind": cfg["kind"], "identity": cfg["identity"],
                         "credentials_problem": cfg["problem"], "verified_at": st["verified_at"] if st else None, "last_verification": ({"ok": bool(st["ok"]), "code": st["code"], "message": st["message"],
                         "properties_visible": st["sites"]} if st else None), "mapped_clients": len({m["client_id"] for m in maps}), "mappings": len(maps), "how": self.how(src), "missing": missing})
        return rows

    # ------------------------------------------------------------------ live checks
    async def sites(self, source: str, *, fresh: bool = False) -> list[dict]:
        hit = self._sites.get(source)
        if hit and not fresh and hit[0] > self.clock() - SITES_TTL_S:
            return hit[1]
        rows = await self.client(source).list_sites()
        self._sites[source] = (self.clock(), rows)
        return rows

    async def verify(self, source: str) -> dict:
        cli = self.client(source)
        cfg = cli.configured()
        if not cfg["configured"]:
            self.db.set_state(source, False, cfg["code"] or "NOT_CONNECTED", cfg["problem"], 0, None)
            raise ConnectorError(cfg["code"] or "NOT_CONNECTED", cfg["problem"])
        getattr(cli, "reset", lambda: None)()      # verification must prove the stored credentials, not a token cached before they were revoked
        try:
            rows = await self.sites(source, fresh=True)
        except ConnectorError as e:
            self.db.set_state(source, False, e.code, e.message, 0, cfg["kind"])
            raise
        self.db.set_state(source, True, "", "", len(rows), cfg["kind"])
        return {"source": source, "ok": True, "credential_kind": cfg["kind"], "properties_visible": len(rows), "identity": cfg["identity"]}

    # ------------------------------------------------------------------ mapping (owner-controlled)
    @staticmethod
    def _usable(source: str, permission: str) -> bool:
        return permission in QUERYABLE if source == "google_search_console" else permission == "verified"

    async def suggest(self, client_id: str, source: str, client_domains: list[str]) -> dict:
        """Properties the credential can use whose domain matches one of THIS client's websites. Never lists other properties."""
        wanted = {domain_of(d) for d in client_domains if d}
        rows = await self.sites(source)
        cands = [{"property": r["property"], "permission": r["permission"], "usable": self._usable(source, r["permission"]), "domain": domain_of(r["property"])}
                 for r in rows if domain_of(r["property"]) in wanted or any(domain_of(r["property"]).endswith("." + w) for w in wanted)]
        taken = {c["property"]: self.db.owner_of(source, c["property"]) for c in cands}
        for c in cands:
            c["mapped_to_this_client"] = taken[c["property"]] == client_id
            c["mapped_to_another_client"] = bool(taken[c["property"]] and taken[c["property"]] != client_id)
        return {"candidates": cands, "client_domains": sorted(wanted), "mapped": [m["property"] for m in self.db.mappings(client_id=client_id, source=source)]}

    async def map(self, client_id: str, source: str, prop: str, *, client_domains: list[str], mapped_by: str, allow_mismatch: bool = False) -> dict:
        rows = await self.sites(source, fresh=True)
        found = next((r for r in rows if r["property"] == prop), None)
        if not found:
            raise ConnectorError("UNAUTHORIZED_PROPERTY", "the credential cannot see that property: add the account to it first (it was not mapped)")
        if not self._usable(source, found["permission"]):
            raise ConnectorError("UNAUTHORIZED_PROPERTY", f"the credential's permission on that property is '{found['permission']}', which cannot read data (it was not mapped)")
        wanted = {domain_of(d) for d in client_domains if d}
        if domain_of(prop) not in wanted and not any(domain_of(prop).endswith("." + w) for w in wanted) and not allow_mismatch:
            raise ConnectorError("DOMAIN_MISMATCH", f"that property's domain ({domain_of(prop)}) is not one of this client's websites ({', '.join(sorted(wanted)) or 'none on file'}): "
                                 "confirm explicitly if it really is theirs (it was not mapped)")
        owner = self.db.owner_of(source, prop)
        if owner and owner != client_id:
            raise ConnectorError("ALREADY_MAPPED", "that property is already mapped to a different client: unmap it there first (a property serves exactly one client)")
        if owner == client_id:
            return next(m for m in self.db.mappings(client_id=client_id, source=source) if m["property"] == prop)
        return self.db.add(client_id, source, prop, found["permission"], mapped_by)

    def unmap(self, map_id: str) -> None:
        self.db.remove(map_id)

    # ------------------------------------------------------------------ use (what a capability calls)
    async def resolve(self, client_id: str, source: str) -> Resolved:
        cfg = self.client(source).configured()
        if not cfg["configured"]:
            raise ConnectorError("NOT_CONNECTED", f"{LABEL[source]} is not connected: {cfg['problem']}")
        maps = self.db.mappings(client_id=client_id, source=source)
        if not maps:
            raise ConnectorError("NOT_MAPPED", f"no {LABEL[source]} property is mapped to this client: the owner maps one in the Knowledge Hub (AI & Approvals -> Data sources); nothing was queried")
        try:
            rows = await self.sites(source)
        except ConnectorError as e:
            self.db.set_state(source, False, e.code, e.message, 0, cfg["kind"])
            raise
        self.db.set_state(source, True, "", "", len(rows), cfg["kind"])   # a successful live read of the credential's properties is proof that it works
        visible = {r["property"]: r["permission"] for r in rows}
        for m in maps:
            perm = visible.get(m["property"])
            if perm and self._usable(source, perm):
                self.db.touch(m["map_id"])
                return Resolved(source, m["property"], perm, m["map_id"])
            self.db.touch(m["map_id"], "no longer accessible" if not perm else f"permission is {perm}")
        raise ConnectorError("UNAUTHORIZED_PROPERTY", f"the credential no longer has usable access to the {LABEL[source]} property mapped to this client: re-add the account in {LABEL[source]}, or re-map")
