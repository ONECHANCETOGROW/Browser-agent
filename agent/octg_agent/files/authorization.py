"""Authorized-folder allow-list.

Rules:
* Only folders the user adds through the local CLI (`python -m octg_agent fs allow <dir>`) are usable. There is deliberately NO HTTP
  endpoint or browser-side way to widen the list, so neither the extension nor a future LLM can grant itself access.
* Every path is resolved with symlinks/junctions followed (`realpath`) and must lie inside an authorized root afterwards.
* Drive roots, the home directory itself and Windows/Program Files directories cannot be authorized.
* Walks (`find`, and the Business Brain's document scan via `safe_walk`) never enter a reparse point: Windows junctions and symlinks are skipped whether or not they
  happen to point back inside the folder, so a link planted inside an authorized folder can neither leak names from outside it nor create a loop. Direct paths are
  accepted only if their FINAL real path is inside a root, which covers nested links, `..`, sibling-prefix names and mixed case.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path


class AuthorizationError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _norm(p: str | os.PathLike) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(p)))


def _inside(child: str, root: str) -> bool:
    try:
        return os.path.commonpath([child, root]) == root
    except ValueError:  # different drives
        return False


_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: Windows junctions, symlinks, mount points, cloud placeholders


def is_reparse_point(path: str | os.PathLike) -> bool:
    """True for a symlink or any Windows reparse point (junction, mount point...). Python 3.11's os.walk and DirEntry.is_symlink() do not report junctions."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def safe_walk(root: str | os.PathLike, max_depth: int = 8, on_skip=None):
    """os.walk over `root` that never enters (or yields) a reparse point and never leaves the real root. Yields (dirpath, dirnames, filenames) like os.walk.
    Everything it yields is a plain directory or file physically inside `root`. `on_skip(path, reason)` is told about every link that was not followed."""
    real_root = _norm(root)
    base_depth = len(Path(real_root).parts)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if len(Path(dirpath).parts) - base_depth >= max_depth:
            dirnames[:] = []
        keep_d, keep_f = [], []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if is_reparse_point(full) or not _inside(_norm(full), real_root):
                if on_skip:
                    on_skip(full, "a link (junction/symlink) was not followed")
            else:
                keep_d.append(d)
        for f in filenames:
            full = os.path.join(dirpath, f)
            if is_reparse_point(full) or not _inside(_norm(full), real_root):
                if on_skip:
                    on_skip(full, "a file link was not followed")
            else:
                keep_f.append(f)
        dirnames[:] = keep_d
        files = keep_f
        yield dirpath, dirnames, files


def _forbidden_roots() -> set[str]:
    out = {_norm(Path.home())}
    for var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        v = os.environ.get(var)
        if v:
            out.add(_norm(v))
    return out


def check_filename(name: str) -> str:
    """A bare, safe file name (no separators, no traversal, no Windows-reserved names)."""
    if not name or name in (".", "..") or _BAD_NAME_CHARS.search(name) or name != name.strip() or name.endswith("."):
        raise AuthorizationError("INVALID_PARAMS", f"Unsafe file name: {name!r}")
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        raise AuthorizationError("INVALID_PARAMS", f"Reserved file name: {name!r}")
    return name


class AuthorizedFolders:
    def __init__(self, store: Path):
        self.store = Path(store)

    # ---- persistence
    def _load(self) -> list[dict]:
        if not self.store.is_file():
            return []
        try:
            return list(json.loads(self.store.read_text(encoding="utf-8")).get("folders", []))
        except (ValueError, OSError):
            return []

    def _save(self, folders: list[dict]) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text(json.dumps({"folders": folders}, indent=2), encoding="utf-8")

    def roots(self) -> list[dict]:
        return self._load()

    def root_paths(self) -> list[str]:
        return [r["path"] for r in self._load()]

    # ---- management (CLI only)
    def allow(self, path: str, label: str | None = None) -> dict:
        p = Path(path)
        if not p.is_dir():
            raise AuthorizationError("FILE_NOT_FOUND", f"Not an existing folder: {path}")
        real = os.path.realpath(p)
        n = os.path.normcase(real)
        if Path(real).parent == Path(real):
            raise AuthorizationError("FILE_NOT_AUTHORIZED", "A whole drive cannot be authorized. Authorize a specific folder.")
        if n in _forbidden_roots():
            raise AuthorizationError("FILE_NOT_AUTHORIZED", "That folder is too broad or is a system folder. Authorize a specific sub-folder.")
        folders = self._load()
        if any(os.path.normcase(f["path"]) == n for f in folders):
            return next(f for f in folders if os.path.normcase(f["path"]) == n)
        entry = {"path": real, "label": label, "added": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        folders.append(entry)
        self._save(folders)
        return entry

    def revoke(self, path: str) -> bool:
        n = _norm(path)
        folders = self._load()
        kept = [f for f in folders if os.path.normcase(f["path"]) != n]
        self._save(kept)
        return len(kept) != len(folders)

    # ---- checks
    def _reject_ads(self, raw: str) -> None:
        if os.name == "nt" and ":" in raw[2:] and not raw.startswith("\\\\?\\"):
            raise AuthorizationError("FILE_NOT_AUTHORIZED", "Alternate data streams are not allowed.")
        if "\x00" in raw:
            raise AuthorizationError("INVALID_PARAMS", "Path contains a NUL character.")

    def resolve(self, raw: str, *, must_exist: bool = True) -> Path:
        """Return the real path if it is inside an authorized folder, else raise FILE_NOT_AUTHORIZED."""
        if not isinstance(raw, str) or not raw.strip():
            raise AuthorizationError("INVALID_PARAMS", "A path is required.")
        self._reject_ads(raw)
        if not os.path.isabs(raw):
            raise AuthorizationError("INVALID_PARAMS", "Use an absolute path (see fs_find / fs_list_roots).")
        real = _norm(raw)
        roots = [os.path.normcase(r) for r in self.root_paths()]
        if not any(_inside(real, r) for r in roots):
            raise AuthorizationError("FILE_NOT_AUTHORIZED", "That path is not inside an authorized folder.", {"authorized_roots": self.root_paths()})
        if must_exist and not os.path.exists(real):
            raise AuthorizationError("FILE_NOT_FOUND", f"No such file or folder: {raw}")
        return Path(os.path.realpath(raw))

    def resolve_file(self, raw: str) -> Path:
        p = self.resolve(raw)
        if not p.is_file():
            raise AuthorizationError("FILE_NOT_FOUND", f"Not a file: {raw}")
        return p

    def resolve_dir(self, raw: str) -> Path:
        p = self.resolve(raw)
        if not p.is_dir():
            raise AuthorizationError("FILE_NOT_FOUND", f"Not a folder: {raw}")
        return p

    # ---- operations
    def find(self, pattern: str, root: str | None = None, max_results: int = 50, max_depth: int = 8) -> list[dict]:
        bases = [self.resolve_dir(root)] if root else [Path(r) for r in self.root_paths() if os.path.isdir(r)]
        out: list[dict] = []
        pat = pattern.lower()
        for base in bases:
            for dirpath, dirnames, filenames in safe_walk(base, max_depth):
                for fn in filenames:
                    if fnmatch.fnmatch(fn.lower(), pat):
                        full = Path(dirpath) / fn
                        try:
                            st = full.stat()
                        except OSError:
                            continue
                        out.append({"path": str(full), "size": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds")})
                        if len(out) >= max_results:
                            return out
        return out

    def read_for_upload(self, raw: str, max_bytes: int) -> dict:
        p = self.resolve_file(raw)
        size = p.stat().st_size
        if size > max_bytes:
            raise AuthorizationError("FILE_TOO_LARGE", f"{p.name} is {size} bytes; the limit is {max_bytes}.", {"size": size, "limit": max_bytes})
        data = p.read_bytes()
        if _norm(self.resolve_file(raw)) != _norm(p):  # the path was re-pointed (junction/symlink swap) while the file was being read: refuse
            raise AuthorizationError("FILE_NOT_AUTHORIZED", "The file's location changed while it was being read.")
        return {"path": str(p), "name": p.name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "data": data}

    def save_into(self, dest_dir: str, filename: str, src: str | os.PathLike, overwrite: bool = False) -> dict:
        d = self.resolve_dir(dest_dir)
        name = check_filename(filename)
        target = d / name
        if _norm(target.parent) != _norm(d):  # defence in depth against odd names
            raise AuthorizationError("FILE_NOT_AUTHORIZED", "Destination escapes the authorized folder.")
        if os.path.lexists(target) and os.path.islink(target):
            raise AuthorizationError("FILE_NOT_AUTHORIZED", "Refusing to write through a symbolic link.")
        if target.exists() and not overwrite:
            raise AuthorizationError("FILE_EXISTS", f"{name} already exists (use overwrite=true).")
        src_path = Path(src)
        if not src_path.is_file():
            raise AuthorizationError("FILE_NOT_FOUND", f"Source file is missing: {src}")
        tmp = d / f".{name}.octg-part"
        shutil.copyfile(src_path, tmp)
        os.replace(tmp, target)
        data = target.read_bytes()
        return {"saved_path": str(target), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
