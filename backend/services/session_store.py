"""
Disk-backed session store. Survives backend restarts.

Sessions are kept in memory (cache) and flushed to JSON files under
sessions/. Anthropic SDK content blocks (TextBlock, ToolUseBlock, etc.)
are serialized via model_dump() since they're Pydantic models.
"""
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_SESSION_DIR = Path(__file__).parent.parent.parent / "sessions"
_TTL_SECONDS = 86400 * 2  # 48 hours


def _default(obj: Any) -> Any:
    """JSON fallback: Pydantic models → dict, everything else → str."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


class SessionStore:
    """
    Dict-like store backed by per-session JSON files.

    Reads hit the in-memory cache first. Writes (via __setitem__ or save())
    flush to disk immediately so restarts don't lose state.
    """

    def __init__(self):
        _SESSION_DIR.mkdir(exist_ok=True)
        self._cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Public interface (mirrors dict enough for existing call sites)
    # ------------------------------------------------------------------

    def get(self, session_id: str, default=None):
        if session_id in self._cache:
            return self._cache[session_id]
        return self._load(session_id) or default

    def __contains__(self, session_id: str) -> bool:
        return self.get(session_id) is not None

    def __setitem__(self, session_id: str, data: dict):
        self._cache[session_id] = data
        self._write(session_id, data)

    def __getitem__(self, session_id: str):
        val = self.get(session_id)
        if val is None:
            raise KeyError(session_id)
        return val

    def save(self, session_id: str):
        """Flush the in-memory session to disk after in-place mutations."""
        data = self._cache.get(session_id)
        if data is not None:
            self._write(session_id, data)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _path(self, session_id: str) -> Path:
        return _SESSION_DIR / f"{session_id}.json"

    def _write(self, session_id: str, data: dict):
        p = self._path(session_id)
        try:
            payload = dict(data)
            payload["_ts"] = time.time()
            p.write_text(json.dumps(payload, default=_default))
        except Exception as exc:
            log.warning("Session write failed for %s: %s", session_id, exc)

    def _load(self, session_id: str) -> Optional[dict]:
        p = self._path(session_id)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
            if time.time() - data.pop("_ts", 0) > _TTL_SECONDS:
                p.unlink(missing_ok=True)
                return None
            self._cache[session_id] = data
            log.info("Session %s restored from disk", session_id)
            return data
        except Exception as exc:
            log.warning("Session read failed for %s: %s", session_id, exc)
            return None
