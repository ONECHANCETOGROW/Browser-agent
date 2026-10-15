"""Imports that have been read and compared but not yet confirmed. They live in this process's memory only (raw protected values included, registered with the log scrubber) and expire; a restart forgets them.
Anything the user accepts or rejects is written to the database as an import record (masks only)."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from ..business.spreadsheet import ImportRefused, Preview

TTL_S = 2 * 3600
MAX_STAGED = 12


@dataclass
class Staged:
    sid: str
    kind: str          # spreadsheet | markdown
    name: str
    scope: str         # "" for a workbook (it names its clients), "me" or a client id for Markdown
    preview: Preview
    created: float

    @property
    def status(self) -> str:
        return "Waiting for confirmation" if not self.preview.blocking(accept_errors=True) else "Preview"


class StagedImports:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._items: dict[str, Staged] = {}

    def sweep(self) -> None:
        now = self._clock()
        for sid, st in list(self._items.items()):
            if now - st.created > TTL_S:
                self.remove(sid)

    def add(self, kind: str, name: str, scope: str, preview: Preview) -> Staged:
        self.sweep()
        while len(self._items) >= MAX_STAGED:
            self.remove(min(self._items.values(), key=lambda s: s.created).sid)
        st = Staged("stg_" + uuid.uuid4().hex[:10], kind, name, scope, preview, self._clock())
        self._items[st.sid] = st
        return st

    def get(self, sid: str) -> Staged:
        self.sweep()
        st = self._items.get(sid)
        if st is None:
            raise ImportRefused("NO_SUCH_PREVIEW", "that preview has expired or was already finished: read the file again")
        return st

    def remove(self, sid: str) -> None:
        st = self._items.pop(sid, None)
        if st is not None:
            st.preview.close()

    def list(self) -> list[Staged]:
        self.sweep()
        return sorted(self._items.values(), key=lambda s: s.created, reverse=True)

    def close_all(self) -> None:
        for sid in list(self._items):
            self.remove(sid)
