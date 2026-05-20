"""mem-reflection-hermes plugin — Self-evolving memory & reflection system.

Ported from https://github.com/coder-brzhang/small-rust-hermes

Features:
- Structured memories: Markdown + YAML frontmatter (id, created, source,
  confidence, pinned, tags, supersedes)
- Dual scope: user (~/.hermes/memories/) and project (./.hermes/memories/)
- TF-IDF relevance search (zero-dependency, fast)
- Optional embedding search (via ONNX Runtime, lazy-loaded)
- Conflict detection on write
- Micro-reflection: lightweight per-turn background reflection
- Full reflection: session-end structured JSON pipeline with human approval
- Skill auto-matching: token-overlap + optional embedding hybrid
- Context layering: Pinned → Active Index → Triggered Skills

All data lives in flat files for transparency and version-control friendliness.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config & paths (with caching)
# ---------------------------------------------------------------------------

# Cached config to avoid repeated YAML parsing
_cached_config: Optional[Dict[str, Any]] = None
_cached_config_mtime: float = 0.0

# Configuration keys (centralized for easy customization)
_CONFIG_SECTION = "mem_reflection_hermes"
_CONFIG_KEY_EMBEDDINGS = "embeddings"
_CONFIG_KEY_MICRO_REFLECTION = "micro_reflection"
_CONFIG_KEY_REFLECTION_MODE = "reflection_mode"
_CONFIG_KEY_PALACE_MODE = "palace_mode"
_CONFIG_KEY_PROFILE_MODE = "profile_mode"
_CONFIG_KEY_PALACE_INSTRUCTIONS = "palace_instructions"
_CONFIG_KEY_ACTIVE_MEMORY_CAP = "active_memory_index_cap"
_CONFIG_KEY_SKILL_INDEX_CAP = "skill_index_cap"
_CONFIG_KEY_RELEVANT_MEMORY_CAP = "relevant_memory_cap"
_CONFIG_KEY_TRIGGERED_SKILL_CAP = "triggered_skill_cap"


def _hermes_home() -> Path:
    """Resolve Hermes home directory.

    Priority:
    1. HERMES_HOME environment variable
    2. hermes_constants.get_hermes_home() (if available)
    3. ~/.hermes (default)
    """
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        return Path(env_home)
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except Exception:
        return Path.home() / ".hermes"


def _load_config() -> Dict[str, Any]:
    """Load Hermes config.yaml with caching (reloads if file changed)."""
    global _cached_config, _cached_config_mtime
    cfg_path = _hermes_home() / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        mtime = cfg_path.stat().st_mtime
        if _cached_config is not None and mtime == _cached_config_mtime:
            return _cached_config
        import yaml
        with open(cfg_path, "r", encoding="utf-8") as f:
            _cached_config = yaml.safe_load(f) or {}
        _cached_config_mtime = mtime
        return _cached_config
    except Exception:
        return {}


def _plugin_config() -> Dict[str, Any]:
    """Get plugin-specific configuration section."""
    cfg = _load_config()
    plugins_cfg = cfg.get("plugins", {})
    return plugins_cfg.get(_CONFIG_SECTION, {})


def _embeddings_enabled() -> bool:
    """Check if embeddings are enabled in config."""
    return bool(_plugin_config().get(_CONFIG_KEY_EMBEDDINGS, True))


def _micro_reflection_enabled() -> bool:
    """Check if micro-reflection is enabled in config."""
    return bool(_plugin_config().get(_CONFIG_KEY_MICRO_REFLECTION, False))


def _reflection_mode() -> str:
    """Reflection mode: 'embedding' (local, default), 'llm' (expensive), or 'hybrid'."""
    return str(_plugin_config().get(_CONFIG_KEY_REFLECTION_MODE, "embedding"))


def _palace_mode_enabled() -> bool:
    """Check if Memory Palace mode is enabled (zone-based, tool-driven retrieval)."""
    return bool(_plugin_config().get(_CONFIG_KEY_PALACE_MODE, True))


def _profile_mode_enabled() -> bool:
    """Check if compiled profile mode is enabled (LLM-compiled, all-in-one injection)."""
    return bool(_plugin_config().get(_CONFIG_KEY_PROFILE_MODE, False))


def _palace_instructions_enabled() -> bool:
    """Check if palace usage instructions should be included in context."""
    return bool(_plugin_config().get(_CONFIG_KEY_PALACE_INSTRUCTIONS, True))


def _active_memory_cap() -> int:
    """Max episodic memories in the Active memory index section (default 50)."""
    return int(_plugin_config().get(_CONFIG_KEY_ACTIVE_MEMORY_CAP, 50))


def _skill_index_cap() -> int:
    """Max skills in the Available skills index (default 50)."""
    return int(_plugin_config().get(_CONFIG_KEY_SKILL_INDEX_CAP, 50))


def _relevant_memory_cap() -> int:
    """Max per-turn memory bodies injected in legacy mode (default 3)."""
    return int(_plugin_config().get(_CONFIG_KEY_RELEVANT_MEMORY_CAP, 3))


def _triggered_skill_cap() -> int:
    """Max skills expanded per turn (default 3)."""
    return int(_plugin_config().get(_CONFIG_KEY_TRIGGERED_SKILL_CAP, 3))


# ---------------------------------------------------------------------------
# CJK-aware token estimation (mirrors small-rust-hermes compaction.rs)
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """Estimate token count with CJK awareness (fast bytes-based, P1-1).

    CJK/Unicode text → ~3 bytes per token.
    ASCII text → ~4 bytes per token.
    The hybrid byte-count approach is ~600x faster than char-by-char CJK range checks
    while staying within ±15% of tiktoken cl100k_base for mixed CJK+English text.
    """
    if not text:
        return 0
    encoded = text.encode("utf-8")
    n_bytes = len(encoded)
    # Fast path: mostly ASCII text
    if n_bytes <= len(text) * 1.2:
        return (n_bytes + 3) // 4
    # Mixed CJK: UTF-8 multi-byte characters use 3 bytes each → ~1.5 chars/token
    return (n_bytes + 2) // 3


_PALACE_USAGE_INSTRUCTIONS = """## Memory Palace
Your persistent memory is organized in a Memory Palace with zones.
- Use `srh_palace_zones` to see available zones and their counts
- Use `srh_palace_read_zone` to load all memories from a specific zone
- Use `srh_palace_recall` to search by topic, optionally scoped to a zone
- Use `srh_memory_write` (with zone parameter) to persist new learnings
- Use `srh_memory_delete` to remove outdated memories
Don't guess about preferences or conventions — load the relevant zone first."""


# ---------------------------------------------------------------------------
# Palace Index & Zone Cache (mirrors small-rust-hermes palace.rs)
# ---------------------------------------------------------------------------

def _palace_index_path() -> Path:
    """Path to palace-index.md cache file."""
    return _plugin_data_dir() / "palace-index.md"


def _zone_cache_dir() -> Path:
    """Path to zone-cache directory for per-zone summaries."""
    d = _plugin_data_dir() / "zone-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize_zone_filename(zone: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", zone)


def _fast_hash(text: str) -> str:
    """Fast non-crypto hash for write-on-change comparison (P0-1)."""
    return hashlib.blake2b(text.encode(), digest_size=8).hexdigest()


def build_palace_index(memories: List[LoadedMemory]) -> str:
    """Generate a code-based palace index (no LLM needed).

    Groups memories by zone, shows counts and first-line previews.
    Typically ~200-400 tokens.
    """
    groups: Dict[str, List[LoadedMemory]] = {}
    for m in memories:
        groups.setdefault(m.frontmatter.zone, []).append(m)

    if not groups:
        return "## Memory Palace\nEmpty — no memories yet."

    total = len(memories)
    buf = f"## Memory Palace\n{total} memories across {len(groups)} zones. Use srh_palace_read_zone to load details.\n"

    # Zones in consistent order: core > work > episode > general > custom
    zone_order = ["core", "work", "episode", "general"]
    sorted_zones = sorted(groups.keys(), key=lambda z: (
        (zone_order.index(z), z) if z in zone_order else (99, z)
    ))

    for zone in sorted_zones:
        mems = groups[zone]
        # Re-sort: core/work zone zones by predefined order
        if zone in zone_order:
            idx = zone_order.index(zone)
        else:
            idx = 99
        buf += f"\n### {zone} ({len(mems)})\n"
        for m in mems[:5]:
            line = m.body.split("\n")[0].strip()[:80]
            buf += f"- {line}\n"
        if len(mems) > 5:
            buf += f"- ... ({len(mems) - 5} more)\n"
    return buf


def load_zone_summary(zone: str) -> Optional[str]:
    """Load a cached zone summary if available."""
    safe = _sanitize_zone_filename(zone)
    path = _zone_cache_dir() / f"{safe}.md"
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8").strip()
    return content or None


def save_zone_summary(zone: str, content: str) -> Path:
    """Save a zone summary atomically (tmp + rename)."""
    safe = _sanitize_zone_filename(zone)
    d = _zone_cache_dir()
    path = d / f"{safe}.md"
    tmp = d / f".{safe}.md.tmp"
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(path)
    return path


def _plugin_data_dir() -> Path:
    """Plugin data directory (for pending skills, logs, etc.)."""
    # Use the directory containing this file as the plugin root
    plugin_root = Path(__file__).parent.resolve()
    return plugin_root


def _user_memories_dir() -> Path:
    """User-level memories directory."""
    return _hermes_home() / "memories"


def _project_memories_dir() -> Optional[Path]:
    """Project-level memories directory (only if .hermes/ exists in cwd)."""
    p = Path.cwd() / ".hermes" / "memories"
    return p if p.exists() else None


def _user_skills_dir() -> Path:
    """User-level skills directory."""
    return _hermes_home() / "skills"


def _project_skills_dir() -> Optional[Path]:
    """Project-level skills directory (only if .hermes/ exists in cwd)."""
    p = Path.cwd() / ".hermes" / "skills"
    return p if p.exists() else None


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

# Predefined memory zones — mirrors small-rust-hermes zone taxonomy
_ZONE_CORE = "core"
_ZONE_WORK = "work"
_ZONE_EPISODE = "episode"
_ZONE_GENERAL = "general"
_VALID_ZONES = frozenset({_ZONE_CORE, _ZONE_WORK, _ZONE_EPISODE, _ZONE_GENERAL})
# Project zones: any string starting with "project:" is valid
_PROJECT_ZONE_PREFIX = "project:"

def _normalize_zone(zone: Optional[str]) -> str:
    """Normalize a zone string to a valid zone."""
    if not zone:
        return _ZONE_GENERAL
    zone = zone.strip().lower()
    if zone in _VALID_ZONES or zone.startswith(_PROJECT_ZONE_PREFIX):
        return zone
    return _ZONE_GENERAL


@dataclass
class MemoryFrontmatter:
    id: str
    created: str  # ISO-8601
    source: str  # reflection | user | imported
    confidence: str  # low | medium | high
    pinned: bool = False
    tags: List[str] = field(default_factory=list)
    supersedes: List[str] = field(default_factory=list)
    zone: str = "general"  # core | work | episode | general | project:<name>

    @staticmethod
    def new(source: str = "reflection", confidence: str = "medium", tags: Optional[List[str]] = None,
            zone: str = "general") -> "MemoryFrontmatter":
        return MemoryFrontmatter(
            id=f"mem_{uuid.uuid4().hex[:16]}",
            created=datetime.now(timezone.utc).isoformat(),
            source=source,
            confidence=confidence,
            pinned=False,
            tags=tags or [],
            supersedes=[],
            zone=_normalize_zone(zone),
        )


@dataclass
class LoadedMemory:
    frontmatter: MemoryFrontmatter
    body: str
    source_path: Path
    scope: str  # "user" | "project"

    def id(self) -> str:
        return self.frontmatter.id


# ---------------------------------------------------------------------------
# Memory Effectiveness Tracking (mirrors small-rust-hermes stats.rs)
# ---------------------------------------------------------------------------

@dataclass
class MemoryStatEntry:
    """A single memory usage event."""
    memory_id: str
    event: str  # "loaded" | "referenced" | "accessed"
    at: str  # ISO-8601 timestamp


@dataclass
class MemoryEffectiveness:
    """Per-memory effectiveness summary — computed from stats.jsonl."""
    loaded: int = 0
    referenced: int = 0
    accessed: int = 0
    last_event_at: Optional[str] = None

    def factor(self) -> float:
        """Effectiveness factor in [0.5, 1.0].

        Memories with no data default to 1.0. Low referenced/loaded
        ratio pulls the factor down toward 0.5.
        """
        if self.loaded == 0:
            return 1.0
        ratio = self.referenced / self.loaded
        return 0.5 + 0.5 * ratio

    def decay_factor(self, now: Optional[datetime] = None) -> float:
        """Decay factor based on time since last access. 30-day half-life, floor 0.3."""
        if self.last_event_at is None:
            return 1.0
        now_dt = now or datetime.now(timezone.utc)
        try:
            last_dt = datetime.fromisoformat(self.last_event_at)
            days = max(0, (now_dt - last_dt).days)
            return max(0.3, 0.5 ** (days / 30.0))
        except Exception:
            return 1.0


def _stats_path() -> Path:
    """Path to memory-stats.jsonl."""
    return _plugin_data_dir() / "memory-stats.jsonl"


def record_memory_stat(memory_id: str, event: str) -> None:
    """Append a memory stat entry to stats.jsonl. Best-effort."""
    _batch_record_stats([(memory_id, event)])


def _batch_record_stats(entries: List[Tuple[str, str]]) -> None:
    """Append multiple stat entries in a single file open. Best-effort."""
    _stat_queue.put(entries)  # P1-2: async flush via background thread


# P1-2: Background stat writer — avoids blocking the hot path on JSONL fsync
import atexit as _atexit

_stat_queue: "queue.Queue[List[Tuple[str, str]]]" = queue.Queue()

def _stat_flush_worker() -> None:
    """Drain the stat queue and append to JSONL in a single file open per batch."""
    while True:
        try:
            batch = _stat_queue.get(timeout=1)
        except Exception:
            continue  # Timeout, loop again
        if batch is None:
            break  # Shutdown signal
        try:
            now = datetime.now(timezone.utc).isoformat()
            sp = _stats_path()
            sp.parent.mkdir(parents=True, exist_ok=True)
            with open(sp, "a", encoding="utf-8") as f:
                for memory_id, event in batch:
                    f.write(json.dumps({
                        "memory_id": memory_id,
                        "event": event,
                        "at": now,
                    }, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("Failed to batch record %d memory stats", len(batch))

_stat_thread = threading.Thread(target=_stat_flush_worker, daemon=True)
_stat_thread.start()

def _shutdown_stat_writer() -> None:
    """Flush remaining stats on process exit."""
    _stat_queue.put(None)  # Signal shutdown
    _stat_thread.join(timeout=2)

_atexit.register(_shutdown_stat_writer)


def load_effectiveness() -> Dict[str, MemoryEffectiveness]:
    """Load effectiveness stats for all memories from stats.jsonl.

    Returns a dict mapping memory_id → MemoryEffectiveness.
    """
    sp = _stats_path()
    if not sp.exists():
        return {}
    eff: Dict[str, MemoryEffectiveness] = {}
    try:
        with open(sp, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                mid = entry.get("memory_id", "")
                if not mid:
                    continue
                e = eff.setdefault(mid, MemoryEffectiveness())
                ev = entry.get("event", "")
                if ev == "loaded":
                    e.loaded += 1
                elif ev == "referenced":
                    e.referenced += 1
                elif ev == "accessed":
                    e.accessed += 1
                at = entry.get("at")
                if at and (e.last_event_at is None or at > e.last_event_at):
                    e.last_event_at = at
    except Exception:
        logger.debug("Failed to load effectiveness stats")
    return eff


@dataclass
class SkillFrontmatter:
    name: str
    description: str
    triggers: List[str] = field(default_factory=list)
    version: Optional[str] = None
    license: Optional[str] = None
    always_active: bool = False  # If True, inject full body into session prompt unconditionally


@dataclass
class LoadedSkill:
    frontmatter: SkillFrontmatter
    body: str
    source_path: Path
    scope: str


# ---------------------------------------------------------------------------
# Frontmatter IO
# ---------------------------------------------------------------------------

def _parse_frontmatter(raw: str) -> Tuple[Dict[str, Any], str]:
    """Parse --- yaml --- body from raw text.

    Uses msgspec for fast YAML parsing if available (8x faster than PyYAML),
    with fallback to PyYAML for edge cases.
    """
    s = raw.strip()
    if s.startswith("\ufeff"):
        s = s[1:]
    if not s.startswith("---"):
        return {}, raw
    after_open = s[3:].lstrip("-\n")
    close_idx = after_open.find("\n---")
    if close_idx == -1:
        return {}, raw
    yaml_part = after_open[:close_idx]
    body_part = after_open[close_idx + 4:].lstrip("-\n")

    # Try msgspec first (fast path)
    try:
        import msgspec

        class _FrontmatterStruct(msgspec.Struct):
            id: str = ""
            created: str = ""
            source: str = "conversation"
            confidence: str = "medium"
            pinned: bool = False
            tags: List[str] = []
            supersedes: List[str] = []
            zone: str = "general"
            always_active: bool = False

        # msgspec doesn't auto-parse datetime strings, so we pre-process
        decoded = msgspec.yaml.decode(yaml_part, type=_FrontmatterStruct)
        data = {
            "id": decoded.id,
            "created": decoded.created,
            "source": decoded.source,
            "confidence": decoded.confidence,
            "pinned": decoded.pinned,
            "tags": decoded.tags,
            "supersedes": decoded.supersedes,
            "zone": decoded.zone or "general",
            "always_active": decoded.always_active,
        }
        return data, body_part
    except Exception:
        pass

    # Fallback to PyYAML
    try:
        import yaml
        data = yaml.safe_load(yaml_part) or {}
    except Exception:
        data = {}
    return data, body_part


def _serialize_frontmatter(data: Dict[str, Any], body: str) -> str:
    try:
        import yaml
        yaml_text = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except Exception:
        yaml_text = ""
    yaml_text = yaml_text.strip()
    body_clean = body.strip()
    return f"---\n{yaml_text}\n---\n\n{body_clean}\n"


def _read_memory(path: Path, scope: str) -> Optional[LoadedMemory]:
    try:
        raw = path.read_text(encoding="utf-8")
        data, body = _parse_frontmatter(raw)
        fm = MemoryFrontmatter(
            id=data.get("id", ""),
            created=data.get("created", ""),
            source=data.get("source", "user"),
            confidence=data.get("confidence", "medium"),
            pinned=bool(data.get("pinned", False)),
            tags=data.get("tags", []),
            supersedes=data.get("supersedes", []),
            zone=_normalize_zone(data.get("zone", "general")),
        )
        return LoadedMemory(frontmatter=fm, body=body.strip(), source_path=path, scope=scope)
    except Exception as e:
        logger.warning("Failed to read memory %s: %s", path, e)
        return None


def _write_memory(path: Path, fm: MemoryFrontmatter, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "id": fm.id,
        "created": fm.created,
        "source": fm.source,
        "confidence": fm.confidence,
        "pinned": fm.pinned,
        "tags": fm.tags,
        "supersedes": fm.supersedes,
        "zone": fm.zone,
    }
    path.write_text(_serialize_frontmatter(data, body), encoding="utf-8")


# P2-2: Async file writer — avoids blocking agent on disk I/O
_write_queue: "queue.Queue[Tuple[Path, str]]" = queue.Queue()
_pending_writes: Set[Path] = set()  # Track files being written (for delete safety)

def _file_flush_worker() -> None:
    """Drain write queue in background, writing files to disk."""
    while True:
        try:
            item = _write_queue.get(timeout=1)
        except Exception:
            continue
        if item is None:
            break
        path, content = item
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except Exception:
            logger.debug("Async write failed for %s", path)
        finally:
            _pending_writes.discard(path)

_write_thread = threading.Thread(target=_file_flush_worker, daemon=True)
_write_thread.start()

def _async_write_memory(path: Path, fm: MemoryFrontmatter, body: str) -> None:
    """Submit memory file write to background thread (P2-2)."""
    data = {
        "id": fm.id,
        "created": fm.created,
        "source": fm.source,
        "confidence": fm.confidence,
        "pinned": fm.pinned,
        "tags": fm.tags,
        "supersedes": fm.supersedes,
        "zone": fm.zone,
    }
    content = _serialize_frontmatter(data, body)
    _pending_writes.add(path)
    _write_queue.put((path, content))

def _shutdown_file_writer() -> None:
    """Flush remaining file writes on process exit."""
    _write_queue.put(None)
    _write_thread.join(timeout=5)

_atexit.register(_shutdown_file_writer)


# ---------------------------------------------------------------------------
# Memory Store
# ---------------------------------------------------------------------------

class MemoryStore:
    def __init__(self, user_root: Path, project_root: Optional[Path] = None):
        self.user_root = user_root
        self.project_root = project_root
        self._embed_index: Optional[Any] = None
        self._embed_lock = threading.Lock()
        self._effectiveness_cache: Optional[Dict[str, MemoryEffectiveness]] = None  # lazy-loaded from JSONL
        self._doc_tokens: Optional[List[Tuple[str, List[str]]]] = None  # cached (id, tokens) for TF-IDF
        self._cache: Dict[str, Any] = {}  # In-memory cache
        self._cache_valid = False
        self._id_to_path: Dict[str, Path] = {}  # O(1) delete: memory id → file path
        self._index_dirty: bool = True  # P2-1: event-driven palace index rebuild
        self._last_index_hash: str = ""  # P0-1: write-on-change
        self._cached_index: str = ""  # Cached built index string (avoids rebuild on warm path)

    # -- listing --------------------------------------------------------------

    def _invalidate_cache(self) -> None:
        self._cache_valid = False

    def _update_cache_for_put(self, scope: str, fm: MemoryFrontmatter, body: str, path: Path) -> None:
        """Incrementally update cache after put() without re-reading all files."""
        if not self._cache_valid:
            return  # Will be rebuilt on next access
        loaded = LoadedMemory(frontmatter=fm, body=body.strip(), source_path=path, scope=scope)
        # O(1) id→path index
        self._id_to_path[fm.id] = path
        # Insert into 'all' maintaining sort order
        all_mems = self._cache["all"]
        # Find insertion point
        inserted = False
        for i, m in enumerate(all_mems):
            if m.id() > fm.id:
                all_mems.insert(i, loaded)
                inserted = True
                break
        if not inserted:
            all_mems.append(loaded)
        # Update active if not superseded
        if fm.id not in self._cache["superseded"]:
            self._cache["active"].append(loaded)
            if fm.pinned:
                self._cache["pinned"].append(loaded)
        # Update superseded set
        for old_id in fm.supersedes:
            self._cache["superseded"].add(old_id)
            # Remove superseded from active/pinned
            self._cache["active"] = [m for m in self._cache["active"] if m.id() != old_id]
            self._cache["pinned"] = [m for m in self._cache["pinned"] if m.id() != old_id]
        self._doc_tokens = None  # invalidate on mutation
        self._index_dirty = True  # P2-1: mark palace index for rebuild
        self._cached_index = ""  # Invalidate cached index

    def _update_cache_for_delete(self, mem_id: str) -> None:
        """Incrementally update cache after delete() without re-reading all files."""
        if not self._cache_valid:
            return
        self._cache["all"] = [m for m in self._cache["all"] if m.id() != mem_id]
        self._cache["active"] = [m for m in self._cache["active"] if m.id() != mem_id]
        self._cache["pinned"] = [m for m in self._cache["pinned"] if m.id() != mem_id]
        self._cache["superseded"].discard(mem_id)
        self._doc_tokens = None  # invalidate on mutation
        self._id_to_path.pop(mem_id, None)  # P0-2: clean up id→path index
        self._index_dirty = True  # P2-1: mark palace index for rebuild
        self._cached_index = ""  # Invalidate cached index

    def _ensure_cache(self) -> None:
        if self._cache_valid:
            return
        all_mems: List[LoadedMemory] = []
        self._id_to_path.clear()  # P0-2: rebuild id→path index
        for scope, root in (("user", self.user_root), ("project", self.project_root)):
            if root is None or not root.exists():
                continue
            for f in root.iterdir():
                if f.suffix == ".md":
                    m = _read_memory(f, scope)
                    if m:
                        all_mems.append(m)
                        self._id_to_path[m.id()] = f  # P0-2: populate O(1) index
        all_mems.sort(key=lambda m: m.id())

        superseded: Set[str] = set()
        for m in all_mems:
            for old in m.frontmatter.supersedes:
                superseded.add(old)

        self._cache = {
            "all": all_mems,
            "active": [m for m in all_mems if m.id() not in superseded],
            "pinned": [m for m in all_mems if m.frontmatter.pinned and m.id() not in superseded],
            "superseded": superseded,
        }
        self._cache_valid = True

    def list(self) -> List[LoadedMemory]:
        self._ensure_cache()
        return list(self._cache["all"])

    def list_active(self) -> List[LoadedMemory]:
        self._ensure_cache()
        return list(self._cache["active"])

    def list_pinned(self) -> List[LoadedMemory]:
        self._ensure_cache()
        return list(self._cache["pinned"])

    def list_by_zone(self, zone: str) -> List[LoadedMemory]:
        """Return all active memories in a given zone."""
        return [m for m in self.list_active() if m.frontmatter.zone == zone]

    def group_by_zone(self) -> Dict[str, List[LoadedMemory]]:
        """Group active memories by zone, returning a dict of zone→memories."""
        groups: Dict[str, List[LoadedMemory]] = {}
        for m in self.list_active():
            groups.setdefault(m.frontmatter.zone, []).append(m)
        return groups

    def zone_counts(self) -> Dict[str, int]:
        """Return {zone: count} for all active memories."""
        return {zone: len(mems) for zone, mems in self.group_by_zone().items()}

    def get(self, mem_id: str) -> Optional[LoadedMemory]:
        self._ensure_cache()
        for m in self._cache["all"]:
            if m.id() == mem_id:
                return m
        return None

    # -- write ----------------------------------------------------------------

    def put(self, scope: str, fm: MemoryFrontmatter, body: str) -> Path:
        if self.get(fm.id):
            raise ValueError(f"Duplicate memory id: {fm.id}")
        root = self._root_for(scope)
        date_prefix = fm.created[:10] if fm.created else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        short = fm.id[:16]
        path = root / f"{date_prefix}-{short}.md"
        _async_write_memory(path, fm, body)  # P2-2: async disk I/O
        self._id_to_path[fm.id] = path  # P0-2: O(1) id→path
        self._update_cache_for_put(scope, fm, body, path)
        # Try to index embedding
        self._try_index(fm.id, body)
        return path

    def delete(self, scope: str, mem_id: str) -> bool:
        # P0-2: O(1) lookup via id→path index
        path = self._id_to_path.get(mem_id)
        if path is not None:
            # P2-2: if file is still pending async write, just remove from queue tracking
            _pending_writes.discard(path)
            if path.exists():
                path.unlink()
            self._id_to_path.pop(mem_id, None)
            self._update_cache_for_delete(mem_id)
            self._try_remove_index(mem_id)
            return True
        # Fallback: directory scan (backward compat, if index missed)
        root = self._root_for(scope)
        for f in root.iterdir():
            if f.suffix != ".md":
                continue
            m = _read_memory(f, scope)
            if m and m.id() == mem_id:
                f.unlink()
                self._id_to_path.pop(mem_id, None)
                self._update_cache_for_delete(mem_id)
                self._try_remove_index(mem_id)
                return True
        return False

    def _root_for(self, scope: str) -> Path:
        if scope == "user":
            return self.user_root
        if scope == "project":
            if self.project_root is None:
                raise ValueError("Project scope requested but no project root configured")
            return self.project_root
        raise ValueError(f"Unknown scope: {scope}")

    # -- search ---------------------------------------------------------------

    def _get_effectiveness(self) -> Dict[str, MemoryEffectiveness]:
        """Lazy-load effectiveness stats from JSONL. Cached per store instance."""
        if self._effectiveness_cache is not None:
            return self._effectiveness_cache
        self._effectiveness_cache = load_effectiveness()
        return self._effectiveness_cache

    def refresh_effectiveness(self) -> None:
        """Force reload of effectiveness stats (call after writing new stats)."""
        self._effectiveness_cache = None

    def _ensure_doc_tokens(self, active: List[LoadedMemory]) -> List[Tuple[str, List[str]]]:
        """Build or return cached tokenized documents for TF-IDF."""
        if self._doc_tokens is not None and len(self._doc_tokens) == len(active):
            # Quick check: same IDs in same order
            if all(self._doc_tokens[i][0] == active[i].id() for i in range(len(active))):
                return self._doc_tokens
        # Rebuild
        self._doc_tokens = [(m.id(), _memory_tokens(m)) for m in active]
        return self._doc_tokens

    def search(self, query: str, k: int = 5, zone: Optional[str] = None) -> List[LoadedMemory]:
        active = self.list_active()
        # Try embedding first if available
        embed_results = self._embed_search(query, k)
        if embed_results is not None:
            id_set = {mid for mid, _ in embed_results}
            results = [m for m in active if m.id() in id_set]
            if zone:
                results = [m for m in results if m.frontmatter.zone == _normalize_zone(zone)]
            return results[:k]
        # Load effectiveness and cached doc_tokens
        effectiveness = self._get_effectiveness()
        doc_tokens = self._ensure_doc_tokens(active)
        return _tfidf_search(active, query, k, effectiveness, doc_tokens)

    def check_conflict(self, body: str, threshold: float = 0.85) -> Optional[Tuple[str, float]]:
        active = self.list_active()
        scored = _tfidf_search_scored(active, body, 1)
        if scored:
            m, score = scored[0]
            if score > threshold:
                return (m.id(), score)
        return None

    # -- optional embedding index ---------------------------------------------

    def _ensure_embed(self) -> bool:
        if not _embeddings_enabled():
            return False
        if self._embed_index is not None:
            return True
        with self._embed_lock:
            if self._embed_index is not None:
                return True
            try:
                self._embed_index = {"vectors": {}, "ids": []}
                # Index existing memories using unified embed function
                for m in self.list_active():
                    vec = _embed_single(m.body)
                    if vec is not None:
                        self._embed_index["vectors"][m.id()] = vec
                        self._embed_index["ids"].append(m.id())
                logger.info("Embedding index initialized with %d memories", len(self._embed_index["ids"]))
                return True
            except Exception as e:
                logger.debug("Embedding index unavailable: %s", e)
                return False

    def _try_index(self, mem_id: str, body: str) -> None:
        if not self._ensure_embed():
            return
        try:
            vec = _embed_single(body)
            if vec is not None:
                self._embed_index["vectors"][mem_id] = vec
                if mem_id not in self._embed_index["ids"]:
                    self._embed_index["ids"].append(mem_id)
        except Exception:
            pass

    def _try_remove_index(self, mem_id: str) -> None:
        if self._embed_index is None:
            return
        self._embed_index["vectors"].pop(mem_id, None)
        if mem_id in self._embed_index["ids"]:
            self._embed_index["ids"].remove(mem_id)

    def _embed_search(self, query: str, k: int) -> Optional[List[Tuple[str, float]]]:
        if not self._ensure_embed():
            return None
        try:
            qvec = _embed_single(query)
            if qvec is None:
                return None
            scores: List[Tuple[str, float]] = []
            for mid, vec in self._embed_index["vectors"].items():
                sim = _cosine_sim(qvec, vec)
                scores.append((mid, sim))
            scores.sort(key=lambda x: x[1], reverse=True)
            return scores[:k]
        except Exception as e:
            logger.debug("Embedding search failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# TF-IDF search (pure Python, zero dependency)
# ---------------------------------------------------------------------------

_MIN_TOKEN_LEN = 2

# Pre-compiled regex for tokenisation
_TOKEN_RE = re.compile(r"[^a-z0-9\u4e00-\u9fff\u3400-\u4dbf\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]+")

# Pre-computed CJK code point ranges for faster check
_CJK_RANGES = [
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0x3000, 0x303F),
    (0x3040, 0x309F),
    (0x30A0, 0x30FF),
    (0xAC00, 0xD7AF),
]


def _is_cjk(c: str) -> bool:
    cp = ord(c)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _tokenise(s: str) -> List[str]:
    lower = s.lower()
    tokens = []
    for segment in _TOKEN_RE.split(lower):
        char_count = len(segment)
        if char_count == 0:
            continue
        if char_count >= _MIN_TOKEN_LEN:
            tokens.append(segment)
        # CJK bigrams
        if char_count >= 2 and any(_is_cjk(c) for c in segment):
            chars = list(segment)
            for i in range(len(chars) - 1):
                tokens.append(chars[i] + chars[i + 1])
    return tokens


def _memory_tokens(m: LoadedMemory) -> List[str]:
    tokens = _tokenise(m.body)
    for tag in m.frontmatter.tags:
        tokens.extend(_tokenise(tag))
    return tokens


def _tfidf_search(memories: List[LoadedMemory], query: str, k: int,
                  effectiveness: Optional[Dict[str, MemoryEffectiveness]] = None,
                  doc_tokens: Optional[List[Tuple[str, List[str]]]] = None) -> List[LoadedMemory]:
    scored = _tfidf_search_scored(memories, query, k, effectiveness, doc_tokens)
    return [m for m, _ in scored]


def _tfidf_search_scored(memories: List[LoadedMemory], query: str, k: int,
                         effectiveness: Optional[Dict[str, MemoryEffectiveness]] = None,
                         doc_tokens: Optional[List[Tuple[str, List[str]]]] = None
                         ) -> List[Tuple[LoadedMemory, float]]:
    if k == 0 or not memories:
        return []
    q_tokens = _tokenise(query)
    if not q_tokens:
        return []
    n = len(memories)
    df: Dict[str, int] = Counter()
    # Use pre-computed doc_tokens cache or compute on the fly
    raw_doc_tokens: List[List[str]]
    if doc_tokens is not None:
        raw_doc_tokens = [tokens for _, tokens in doc_tokens]
    else:
        raw_doc_tokens = [_memory_tokens(m) for m in memories]
    for tokens in raw_doc_tokens:
        for t in set(tokens):
            df[t] += 1
    q_tf = Counter(q_tokens)
    q_vec = {t: (c / len(q_tokens)) * ((n / df[t]) + 1) for t, c in q_tf.items() if df.get(t)}
    scored: List[Tuple[float, LoadedMemory]] = []
    for tokens, m in zip(raw_doc_tokens, memories):
        m_tf = Counter(tokens)
        m_vec = {t: (c / len(tokens)) * ((n / df[t]) + 1) for t, c in m_tf.items() if df.get(t)}
        score = _cosine_similarity(q_vec, m_vec)
        if score > 0:
            # Apply effectiveness factor if available
            if effectiveness:
                eff = effectiveness.get(m.id())
                if eff:
                    score *= eff.factor() * eff.decay_factor()
            scored.append((score, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [(m, s) for s, m in scored[:k]]


def _cosine_similarity(a: Dict[str, float], b: Dict[str, float]) -> float:
    dot = sum(a[k] * b.get(k, 0.0) for k in a)
    norm_a = sum(v * v for v in a.values()) ** 0.5
    norm_b = sum(v * v for v in b.values()) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Skill Store
# ---------------------------------------------------------------------------

class SkillStore:
    def __init__(self, user_root: Path, project_root: Optional[Path] = None):
        self.user_root = user_root
        self.project_root = project_root
        self._cache: Optional[List[LoadedSkill]] = None  # Lazy cache (skills are static per session)

    def list(self) -> List[LoadedSkill]:
        if self._cache is not None:
            return self._cache
        user_skills = self._list_scope(self.user_root, "user")
        project_skills = self._list_scope(self.project_root, "project") if self.project_root else []
        project_names = {s.frontmatter.name for s in project_skills}
        user_skills = [s for s in user_skills if s.frontmatter.name not in project_names]
        out = user_skills + project_skills
        out.sort(key=lambda s: s.frontmatter.name)
        self._cache = out
        return out

    def invalidate_cache(self) -> None:
        """Force reload on next list() call (e.g., after skill changes)."""
        self._cache = None

    def _list_scope(self, root: Optional[Path], scope: str) -> List[LoadedSkill]:
        out: List[LoadedSkill] = []
        if root is None or not root.exists():
            return out
        for d in root.iterdir():
            if not d.is_dir():
                continue
            skill_md = d / "SKILL.md"
            if not skill_md.exists():
                continue
            s = _read_skill(skill_md, scope)
            if s:
                out.append(s)
        return out

    def get(self, name: str) -> Optional[LoadedSkill]:
        if self.project_root:
            p = self.project_root / name / "SKILL.md"
            if p.exists():
                return _read_skill(p, "project")
        p = self.user_root / name / "SKILL.md"
        if p.exists():
            return _read_skill(p, "user")
        return None


def _read_skill(path: Path, scope: str) -> Optional[LoadedSkill]:
    try:
        raw = path.read_text(encoding="utf-8")
        data, body = _parse_frontmatter(raw)
        fm = SkillFrontmatter(
            name=data.get("name", path.parent.name),
            description=data.get("description", ""),
            triggers=data.get("triggers", []),
            version=data.get("version"),
            license=data.get("license"),
            always_active=bool(data.get("always_active", False)),
        )
        return LoadedSkill(frontmatter=fm, body=body.strip(), source_path=path, scope=scope)
    except Exception as e:
        logger.warning("Failed to read skill %s: %s", path, e)
        return None


# ---------------------------------------------------------------------------
# Skill matcher (token overlap + optional embedding)
# ---------------------------------------------------------------------------

_MIN_SKILL_TOKEN = 3
_TOKEN_WEIGHT = 0.4
_EMBED_WEIGHT = 0.6


def _skill_tokenise(s: str) -> Set[str]:
    return {
        t for t in re.split(r"[^a-z0-9]+", s.lower())
        if len(t) >= _MIN_SKILL_TOKEN
    }


def _skill_bag(s: LoadedSkill) -> Set[str]:
    bag: Set[str] = set()
    for t in s.frontmatter.triggers:
        bag.update(_skill_tokenise(t))
    bag.update(_skill_tokenise(s.frontmatter.name))
    bag.update(_skill_tokenise(s.frontmatter.description))
    return bag


def match_skills(skills: List[LoadedSkill], query: str, k: int = 3) -> List[LoadedSkill]:
    q = _skill_tokenise(query)
    if not q:
        return []
    scored: List[Tuple[float, int, LoadedSkill]] = []
    for s in skills:
        bag = _skill_bag(s)
        raw_token = len(q & bag)
        if raw_token == 0:
            continue
        score = raw_token
        scored.append((score, len(s.frontmatter.triggers), s))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [s for _, _, s in scored[:k]]


# ---------------------------------------------------------------------------
# Reflection log
# ---------------------------------------------------------------------------

REFLECT_LOG_PATH = _plugin_data_dir() / "reflect-log.jsonl"


def _append_reflect_log(entry: Dict[str, Any]) -> None:
    try:
        REFLECT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(REFLECT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _recent_reflect_outcomes(n: int = 10) -> List[Dict[str, Any]]:
    try:
        if not REFLECT_LOG_PATH.exists():
            return []
        lines = REFLECT_LOG_PATH.read_text(encoding="utf-8").strip().split("\n")
        out = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return out
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Reflection prompts
# ---------------------------------------------------------------------------

_FULL_REFLECT_SYSTEM = """You are a reflection module for a self-evolving agent. After each completed
session you are given the full transcript plus the agent's current skill /
memory inventory. Your job is to identify three things, and only when each
case truly meets the bar:

1. SKILL CANDIDATES — reusable procedures the agent worked out, with clear
triggers and a self-contained body of instructions in markdown. Only
propose if you would genuinely want the same procedure applied next
time the same situation appears. Skip anything that was a one-shot
exploration.

2. MEMORY CANDIDATES — durable facts, conventions, preferences, or
constraints the agent discovered that should persist across sessions.
One claim per memory. Default `scope` to `user`; pick `project` only
when the fact is specific to the current repo / codebase.

3. CONFLICTS — when a new memory candidate contradicts, duplicates, or
subsumes an existing memory, report a conflict referencing the existing
memory id and proposing resolution options. Use kind "stale" when an
existing memory is factually wrong or outdated.

CRITICAL — the agent's user sees every candidate and must decide. Spammy
proposals erode trust. Default to empty arrays. Prefer false negatives over
false positives. Confidence = "high" should be rare.

Reply with EXACTLY ONE JSON object matching this schema. No prose. No
markdown fences. No commentary.

{
  "summary": "<one sentence summarising what the session accomplished>",
  "skill_candidates": [
    {
      "name": "kebab-case-name",
      "description": "one-line description for matcher",
      "triggers": ["keyword", "phrase"],
      "body": "## Title\n\nFull markdown instructions, multi-line.",
      "rationale": "why this is reusable enough to keep",
      "confidence": "low" | "medium" | "high"
    }
  ],
  "memory_candidates": [
    {
      "fact": "one short statement; one fact per memory",
      "tags": ["rust", "convention"],
      "scope": "user" | "project",
      "confidence": "low" | "medium" | "high",
      "rationale": "why this should persist",
      "supersedes": ["mem_xxxx"]
    }
  ],
  "conflicts": [
    {
      "with": "mem_xxxx",
      "kind": "contradiction" | "redundancy" | "scope_overlap" | "stale",
      "explain": "what the disagreement is",
      "options": ["keep_old", "keep_new", "merge", "scope_split"]
    }
  ]
}"""

_MICRO_REFLECT_SYSTEM = """You are a micro-reflection module. You just observed ONE turn of conversation (user request + assistant response). Decide if anything from this turn is worth persisting as a memory or skill, and whether any existing memory is now stale.

Rules:
- Default to empty arrays. Most turns produce nothing.
- Only propose a memory if the user stated a durable preference, convention, or fact.
- Only propose a skill if the assistant followed a multi-step procedure that would be reusable verbatim next time.
- Never propose more than 1 memory and 1 skill per micro-reflection.
- Confidence should be "low" or "medium" — never "high" for micro-reflection.
- If the conversation reveals that an existing memory is WRONG or OUTDATED, produce a memory_candidates entry with the corrected fact and set `supersedes` to the old memory's id, plus a conflicts entry with kind "stale".

Reply with EXACTLY ONE JSON object:
{
  "summary": "<one sentence>",
  "skill_candidates": [],
  "memory_candidates": [{"fact": "<short statement>", "tags": [], "scope": "user", "confidence": "low|medium", "rationale": "<why>", "supersedes": ["mem_xxx"]}],
  "conflicts": [{"with": "mem_xxx", "kind": "stale", "explain": "<why old memory is wrong>", "options": ["keep_new", "keep_old"]}]
}"""


# ---------------------------------------------------------------------------
# Reflection runner
# ---------------------------------------------------------------------------

def _strip_code_fence(s: str) -> str:
    s = s.strip()
    for prefix in ("```json", "```"):
        if s.startswith(prefix):
            rest = s[len(prefix):].strip()
            if "```" in rest:
                rest = rest[:rest.rfind("```")]
            return rest.strip()
    if "{" in s:
        return s[s.find("{"):]
    return s


def _repair_truncated_json(s: str) -> Optional[str]:
    s = s.strip()
    if not s.startswith("{"):
        return None
    curly = square = 0
    in_str = escape = False
    last_safe = None
    i = 0
    while i < len(s):
        ch = s[i]
        if escape:
            escape = False
            i += 1
            continue
        if ch == "\\" and in_str:
            escape = True
            i += 1
            continue
        if ch == '"':
            in_str = not in_str
            i += 1
            continue
        if in_str:
            i += 1
            continue
        if ch == "{":
            curly += 1
        elif ch == "}":
            curly -= 1
            if curly == 1:
                rest = s[i + 1:].lstrip()
                if rest.startswith(","):
                    last_safe = i + 2
                elif rest.startswith("}"):
                    last_safe = i + 1
        elif ch == "[":
            square += 1
        elif ch == "]":
            square -= 1
            if square == 0 and curly == 1:
                rest = s[i + 1:].lstrip()
                if rest.startswith(","):
                    last_safe = i + 2
                elif rest.startswith("}"):
                    last_safe = i + 1
        i += 1
    if curly <= 0 and square <= 0:
        return None
    repaired = s[:last_safe].rstrip() if last_safe is not None else s
    if repaired.endswith(","):
        repaired = repaired[:-1]
    # Recount
    c2 = s2 = 0
    in_str = False
    for ch in repaired:
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            c2 += 1
        elif ch == "}":
            c2 -= 1
        elif ch == "[":
            s2 += 1
        elif ch == "]":
            s2 -= 1
    for _ in range(max(s2, 0)):
        repaired += "]"
    for _ in range(max(c2, 0)):
        repaired += "}"
    return repaired


def _parse_reflect_output(text: str) -> Optional[Dict[str, Any]]:
    json_str = _strip_code_fence(text)
    try:
        return json.loads(json_str)
    except Exception as first_err:
        repaired = _repair_truncated_json(json_str)
        if repaired:
            try:
                return json.loads(repaired)
            except Exception:
                pass
        logger.warning("Reflection JSON parse failed: %s", first_err)
        return None


# ---------------------------------------------------------------------------
# Plugin state
# ---------------------------------------------------------------------------

_mem_store: Optional[MemoryStore] = None
_skill_store: Optional[SkillStore] = None
_turns_since_reflect: int = 0
_micro_reflect_queue: List[Dict[str, Any]] = []


def _get_mem_store() -> MemoryStore:
    global _mem_store
    if _mem_store is None:
        _mem_store = MemoryStore(_user_memories_dir(), _project_memories_dir())
    return _mem_store


def _get_skill_store() -> SkillStore:
    global _skill_store
    if _skill_store is None:
        _skill_store = SkillStore(_user_skills_dir(), _project_skills_dir())
    return _skill_store


# ---------------------------------------------------------------------------
# Context assembly (Pinned → Active Index → Triggered Skills)
# ---------------------------------------------------------------------------

def _build_context_block(query: str = "") -> str:
    """Build the memory context block injected into the user message.

    Three modes (checked in priority order):
    1. Palace mode: inject palace index (zone map), agent uses tools for retrieval
    2. Profile mode: inject compiled profile.md if available, no per-turn injection
    3. Legacy mode: pinned + active index + per-turn TF-IDF relevance injection
    """
    mem_store = _get_mem_store()
    skill_store = _get_skill_store()
    parts: List[str] = []
    stat_entries: List[Tuple[str, str]] = []  # Batch collect (id, event)

    # Determine mode — cache config lookups
    palace_mode = _palace_mode_enabled()
    profile_mode = _profile_mode_enabled()

    # Pre-load skills once (used in palace index, triggered, always-active)
    all_skills = skill_store.list()

    # ---- Mode 1: Palace (zone-based, tool-driven retrieval) ----
    if palace_mode:
        active = mem_store.list_active()
        if active:
            # P0-1+P2-1: write-on-change + event-driven rebuild
            if mem_store._index_dirty:
                index = build_palace_index(active)
                h = _fast_hash(index)
                if h != mem_store._last_index_hash:
                    _palace_index_path().parent.mkdir(parents=True, exist_ok=True)
                    _palace_index_path().write_text(index, encoding="utf-8")
                    mem_store._last_index_hash = h
                mem_store._index_dirty = False
                mem_store._cached_index = index  # Cache built string
            else:
                # Reuse cached index (don't rebuild, don't write)
                index = mem_store._cached_index
            parts.append(index)
            for m in active:
                stat_entries.append((m.id(), "loaded"))
        else:
            parts.append("## Memory Palace\nEmpty — no memories yet.")

        if _palace_instructions_enabled():
            parts.append(_PALACE_USAGE_INSTRUCTIONS)

        cap = _skill_index_cap()
        if all_skills:
            parts.append("\n## Available skills")
            for s in all_skills[:cap]:
                parts.append(f"- {s.frontmatter.name}: {s.frontmatter.description}")
            if len(all_skills) > cap:
                parts.append(f"- ... ({len(all_skills) - cap} more)")

    # ---- Mode 2: Compiled Profile (LLM-compiled, all-in-one) ----
    elif profile_mode:
        profile_path = _plugin_data_dir() / "profile.md"
        if profile_path.exists():
            profile = profile_path.read_text(encoding="utf-8").strip()
            if profile:
                parts.append("## User Profile\n")
                parts.append(profile)

        if not parts:
            pinned = mem_store.list_pinned()
            if pinned:
                parts.append("=== Pinned memories (always relevant) ===")
                for m in pinned:
                    parts.append(f"- [{m.id()}] {m.body[:200]}")
                    stat_entries.append((m.id(), "loaded"))
                parts.append("")

    # ---- Mode 3: Legacy (pinned + active index + per-turn TF-IDF) ----
    else:
        pinned = mem_store.list_pinned()
        if pinned:
            parts.append("=== Pinned memories (always relevant) ===")
            for m in pinned:
                parts.append(f"- [{m.id()}] {m.body[:200]}")
                stat_entries.append((m.id(), "loaded"))
            parts.append("")

        if query:
            active = mem_store.search(query, k=_relevant_memory_cap())
        else:
            active = mem_store.list_active()[:_active_memory_cap()]
        if active:
            parts.append("=== Relevant memories ===")
            for m in active:
                if m not in pinned:
                    parts.append(f"- [{m.id()}] {m.body[:200]}")
                    stat_entries.append((m.id(), "loaded"))
            parts.append("")

    # Triggered skills (legacy/profile fallback)
    if not palace_mode or (palace_mode and not parts):
        if query:
            skills = match_skills(all_skills, query, k=_triggered_skill_cap())
        else:
            skills = []
        if skills:
            parts.append("=== Triggered skills ===")
            for s in skills:
                parts.append(f"- {s.frontmatter.name}: {s.frontmatter.description}")
            parts.append("")

    # Always-active skills (all modes)
    always_active = [s for s in all_skills if s.frontmatter.always_active]
    if always_active:
        parts.append("\n## Always-Active Skills\n")
        for s in always_active:
            parts.append(f"### {s.frontmatter.name}\n{s.body.strip()}\n")

    # Flush all stat entries in one file open
    if stat_entries:
        _batch_record_stats(stat_entries)

    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _tool_srh_memory_search(args: dict) -> str:
    query = args.get("query", "")
    k = int(args.get("k", 5))
    zone_filter = args.get("zone")  # Optional zone scope
    mem_store = _get_mem_store()
    results = mem_store.search(query, k)
    # Apply zone filter if specified
    if zone_filter:
        results = [m for m in results if m.frontmatter.zone == _normalize_zone(zone_filter)][:k]
    out = []
    for m in results:
        out.append({
            "id": m.id(),
            "scope": m.scope,
            "confidence": m.frontmatter.confidence,
            "pinned": m.frontmatter.pinned,
            "tags": m.frontmatter.tags,
            "zone": m.frontmatter.zone,
            "body": m.body[:500],
        })
        record_memory_stat(m.id(), "accessed")
    return json.dumps({"results": out}, ensure_ascii=False)


def _tool_srh_memory_write(args: dict) -> str:
    mem_store = _get_mem_store()
    body = args.get("body", "").strip()
    if not body:
        return json.dumps({"error": "body is required"})
    scope = args.get("scope", "user")
    confidence = args.get("confidence", "medium")
    tags = args.get("tags", [])
    pinned = bool(args.get("pinned", False))
    supersedes = args.get("supersedes", [])
    zone = _normalize_zone(args.get("zone"))

    # Conflict check
    conflict = mem_store.check_conflict(body)
    if conflict:
        existing_id, score = conflict
        return json.dumps({
            "error": f"Conflict detected with {existing_id} (similarity {score:.2f}). Use supersedes to override.",
            "conflict_with": existing_id,
            "similarity": score,
        })

    fm = MemoryFrontmatter.new(source="user", confidence=confidence, tags=tags, zone=zone)
    fm.pinned = pinned
    fm.supersedes = supersedes
    path = mem_store.put(scope, fm, body)
    return json.dumps({
        "success": True,
        "id": fm.id,
        "path": str(path),
    })


def _tool_srh_memory_delete(args: dict) -> str:
    mem_store = _get_mem_store()
    mem_id = args.get("id", "")
    scope = args.get("scope", "user")
    if not mem_id:
        return json.dumps({"error": "id is required"})
    ok = mem_store.delete(scope, mem_id)
    return json.dumps({"success": ok, "id": mem_id})


def _tool_srh_skill_search(args: dict) -> str:
    query = args.get("query", "")
    k = int(args.get("k", 3))
    skill_store = _get_skill_store()
    skills = match_skills(skill_store.list(), query, k)
    out = []
    for s in skills:
        out.append({
            "name": s.frontmatter.name,
            "description": s.frontmatter.description,
            "triggers": s.frontmatter.triggers,
            "scope": s.scope,
        })
    return json.dumps({"results": out}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Palace tools (zone-based memory navigation)
# ---------------------------------------------------------------------------

def _tool_srh_palace_zones(args: dict) -> str:
    """List all Memory Palace zones with memory counts."""
    mem_store = _get_mem_store()
    groups = mem_store.group_by_zone()
    if not groups:
        return json.dumps({"zones": [], "total": 0, "message": "Memory Palace is empty — no memories yet."})
    zones = []
    total = 0
    for zone, mems in sorted(groups.items()):
        zones.append({"zone": zone, "count": len(mems)})
        total += len(mems)
    return json.dumps({"zones": zones, "total": total}, ensure_ascii=False)


def _tool_srh_palace_read_zone(args: dict) -> str:
    """Load all memories from a specific zone. Returns cached summary if available."""
    zone = _normalize_zone(args.get("zone"))
    mem_store = _get_mem_store()

    # Try cached summary first
    cached = load_zone_summary(zone)
    if cached:
        return json.dumps({
            "zone": zone,
            "source": "cache",
            "content": cached,
        }, ensure_ascii=False)

    # Load raw memories from the zone
    zone_mems = mem_store.list_by_zone(zone)
    if not zone_mems:
        return json.dumps({
            "zone": zone,
            "source": "live",
            "memories": [],
            "message": f"Zone '{zone}' is empty or does not exist.",
        }, ensure_ascii=False)

    # Record access stats
    for m in zone_mems:
        record_memory_stat(m.id(), "accessed")

    memories = []
    for m in zone_mems:
        memories.append({
            "id": m.id(),
            "confidence": m.frontmatter.confidence,
            "pinned": m.frontmatter.pinned,
            "tags": m.frontmatter.tags,
            "body": m.body[:500],
        })
    return json.dumps({
        "zone": zone,
        "source": "live",
        "count": len(memories),
        "memories": memories,
    }, ensure_ascii=False)


def _tool_srh_palace_recall(args: dict) -> str:
    """Search memories by topic, optionally scoped to a zone."""
    query = args.get("topic", "")
    if not query:
        return json.dumps({"error": "topic is required"})
    k = int(args.get("limit", 5))
    zone = _normalize_zone(args.get("zone")) if args.get("zone") else None

    mem_store = _get_mem_store()
    results = mem_store.search(query, k=k * 3)  # Over-fetch for zone filtering

    # Apply zone filter if specified
    if zone:
        results = [m for m in results if m.frontmatter.zone == zone][:k]
    else:
        results = results[:k]

    if not results:
        scope_msg = f" in zone '{zone}'" if zone else ""
        return json.dumps({
            "results": [],
            "message": f"No memories matching '{query}'{scope_msg}",
        }, ensure_ascii=False)

    # Record access stats
    for m in results:
        record_memory_stat(m.id(), "accessed")

    out = []
    for i, m in enumerate(results):
        out.append({
            "rank": i + 1,
            "id": m.id(),
            "zone": m.frontmatter.zone,
            "confidence": m.frontmatter.confidence,
            "tags": m.frontmatter.tags,
            "body": m.body[:500],
        })
    return json.dumps({"results": out}, ensure_ascii=False)


def _tool_srh_reflect_now(args: dict) -> str:
    """Trigger a full reflection on the current session messages."""
    ctx = args.get("ctx")
    messages = args.get("messages", [])
    if not ctx:
        return json.dumps({
            "error": "Reflection requires ctx with LLM access. Run via /reflect slash command or wait for session-end auto-reflection.",
            "recent_outcomes": _recent_reflect_outcomes(5),
        })
    if not messages:
        return json.dumps({"error": "No messages to reflect on"})
    try:
        result = _run_full_reflection(ctx, messages)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Profile Compilation (LLM-compiled memory summary, mirrors small-rust-hermes compile.rs)
# ---------------------------------------------------------------------------

_COMPILE_PROFILE_SYSTEM = """You are a memory curator. Given a list of individual memory entries about a user accumulated over multiple conversations, compile them into a structured profile document.

Rules:
- Use ## markdown headers to organize by topic (categories emerge naturally from the content)
- Merge overlapping or redundant memories into single concise entries
- Use bullet points, one line per point
- Preserve the user's language (Chinese / English as found in entries)
- Drop entries that are trivially obvious or redundant after merging
- Output ONLY the profile markdown, no preamble or explanation"""

_COMPILE_PALACE_INDEX_SYSTEM = """You are a memory curator organizing a Memory Palace index. Given memories grouped by zone, produce a concise zone map.

Rules:
- Use ## Memory Palace as the top header
- Show total memory count and zone count in the first line
- For each zone, use ### zone_name (count) as header
- Under each zone, list 2-3 bullet points summarizing key content
- Keep the entire output under 300 tokens
- Preserve the user's language (Chinese / English as found in entries)
- Output ONLY the index markdown, no preamble"""

_COMPILE_ZONE_SYSTEM = """You are a memory curator. Given all memories from a single zone, compile them into a concise summary.

Rules:
- Use bullet points, one line per point
- Merge overlapping or redundant memories
- Preserve the user's language (Chinese / English as found in entries)
- Keep the output under 400 tokens
- Output ONLY the summary markdown, no preamble"""


def _compile_profile_via_llm(ctx, mode: str = "profile") -> Dict[str, Any]:
    """Compile active memories into a structured markdown document via LLM.

    Args:
        ctx: Hermes agent context with ctx.llm access
        mode: "profile" (profile.md), "palace_index" (palace-index.md), or "zone" (zone-cache/*)

    Returns:
        Dict with 'success', 'path', 'mode', 'token_count' or 'error'
    """
    if not hasattr(ctx, "llm"):
        return {"error": "No LLM available for compilation"}

    mem_store = _get_mem_store()
    active = mem_store.list_active()
    if not active:
        return {"error": "No active memories to compile"}

    try:
        if mode == "profile":
            system = _COMPILE_PROFILE_SYSTEM
            prompt = _build_compile_profile_prompt(active)
            save_path = _plugin_data_dir() / "profile.md"
        elif mode == "palace_index":
            system = _COMPILE_PALACE_INDEX_SYSTEM
            prompt = _build_compile_palace_prompt(active)
            save_path = _palace_index_path()
        elif mode == "zone":
            # Compile all zones
            results = {}
            groups = mem_store.group_by_zone()
            for zone, mems in groups.items():
                prompt = _build_compile_zone_prompt(zone, mems)
                result = ctx.llm.complete_structured(
                    instructions=prompt,
                    input=[{"type": "text", "text": prompt}],
                    system_prompt=_COMPILE_ZONE_SYSTEM,
                    purpose=f"compile_zone_{_sanitize_zone_filename(zone)}",
                    max_tokens=1024,
                )
                if result and not result.error:
                    text = result.text.strip() if hasattr(result, 'text') else str(result)
                    save_zone_summary(zone, text)
                    results[zone] = {"tokens": len(text.split())}
                else:
                    results[zone] = {"error": str(result.error) if result and hasattr(result, 'error') else "unknown"}
            return {"success": True, "mode": "zone", "zones": results}
        else:
            return {"error": f"Unknown compilation mode: {mode}"}

        result = ctx.llm.complete_structured(
            instructions=prompt,
            input=[{"type": "text", "text": prompt}],
            system_prompt=system,
            purpose=f"compile_{mode}",
            max_tokens=4096,
        )

        if not result or result.error:
            return {"error": f"LLM compilation failed: {getattr(result, 'error', 'unknown')}"}

        text = result.text.strip() if hasattr(result, 'text') else str(result)
        if not text:
            return {"error": "LLM returned empty response"}

        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(text, encoding="utf-8")

        return {
            "success": True,
            "mode": mode,
            "path": str(save_path),
            "token_count": len(text.split()),
        }
    except Exception as e:
        logger.warning("Profile compilation failed: %s", e)
        return {"error": str(e)}


def _build_compile_profile_prompt(memories: List[LoadedMemory]) -> str:
    """Build user prompt for profile compilation."""
    buf = "Compile the following memory entries into a structured profile:\n\n"
    for m in memories:
        pin = "pinned, " if m.frontmatter.pinned else ""
        conf = m.frontmatter.confidence
        buf += f"- [{m.id()}] ({pin}{conf}, zone={m.frontmatter.zone}) {m.body.strip()}\n"
    return buf


def _build_compile_palace_prompt(memories: List[LoadedMemory]) -> str:
    """Build user prompt for palace index compilation."""
    groups: Dict[str, List[LoadedMemory]] = {}
    for m in memories:
        groups.setdefault(m.frontmatter.zone, []).append(m)
    buf = "Organize these memories into a palace index:\n\n"
    for zone, mems in sorted(groups.items()):
        buf += f"### {zone} ({len(mems)} memories)\n"
        for m in mems:
            buf += f"- {m.body.strip()}\n"
        buf += "\n"
    return buf


def _build_compile_zone_prompt(zone: str, memories: List[LoadedMemory]) -> str:
    """Build user prompt for zone summary compilation."""
    buf = f"Summarize zone '{zone}' ({len(memories)} memories):\n\n"
    for m in memories:
        buf += f"- ({m.frontmatter.confidence}) {m.body.strip()}\n"
    return buf


def _tool_srh_compile_profile(args: dict) -> str:
    """Compile memories into a structured profile via LLM."""
    ctx = args.get("ctx")
    if not ctx:
        return json.dumps({
            "error": "Compilation requires ctx with LLM access. Use /compile-profile slash command.",
        })
    mode = args.get("mode", "profile")
    result = _compile_profile_via_llm(ctx, mode)
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def _on_session_start(**kwargs) -> None:
    global _turns_since_reflect
    _turns_since_reflect = 0
    logger.debug("mem-reflection-hermes: session started")


def _on_session_end(**kwargs) -> None:
    messages = kwargs.get("messages", [])
    if not messages:
        return
    # Attempt full reflection via LLM if available
    ctx = kwargs.get("ctx")
    if ctx is not None:
        try:
            _run_full_reflection(ctx, messages)
        except Exception as e:
            logger.warning("Full reflection failed: %s", e)
    else:
        logger.info("mem-reflection-hermes: session ended with %d messages — full reflection queued (no ctx)", len(messages))


def _pre_llm_call(**kwargs) -> Optional[Dict[str, str]]:
    """Inject layered context into the user message; also trigger micro-reflection."""
    messages = kwargs.get("messages", [])
    ctx = kwargs.get("ctx")

    # Extract latest user query and assistant response for micro-reflection
    user_msg = ""
    assistant_msg = ""
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and not assistant_msg:
            content = msg.get("content", "")
            if isinstance(content, str):
                assistant_msg = content
        elif msg.get("role") == "user" and not user_msg:
            content = msg.get("content", "")
            if isinstance(content, str):
                user_msg = content
        if user_msg and assistant_msg:
            break

    # Trigger micro-reflection: explicit intent always, otherwise every 3 turns
    # (mirrors small-rust-hermes simplified heuristic)
    if _micro_reflection_enabled() and user_msg and assistant_msg:
        global _turns_since_reflect
        has_intent = _is_explicit_memory_intent(user_msg)
        if has_intent or _turns_since_reflect >= 3:
            try:
                _run_micro_reflection(ctx, user_msg, assistant_msg)
                _turns_since_reflect = 0
            except Exception as e:
                logger.debug("Micro-reflection failed: %s", e)
        else:
            _turns_since_reflect += 1

    # Build context block
    query = ""
    for msg in reversed(messages):
        if msg.get("role") == "user" and msg.get("content"):
            query = msg.get("content", "")
            if isinstance(query, str):
                break
            query = ""
    context = _build_context_block(query)
    if context:
        return {"context": context}
    return None


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register the mem-reflection-hermes plugin."""
    # Register tools
    ctx.register_tool(
        name="srh_memory_search",
        toolset="mem_reflection_hermes",
        schema={
            "name": "srh_memory_search",
            "description": "Search active memories by TF-IDF relevance (or embedding if available). Use 'zone' parameter to filter by zone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "k": {"type": "integer", "description": "Max results", "default": 5},
                    "zone": {"type": "string", "description": "Optional: filter to a specific zone (core/work/episode/general/project:xxx)"},
                },
                "required": ["query"],
            },
        },
        handler=_tool_srh_memory_search,
        description="Search memories by relevance",
        emoji="🧠",
    )
    ctx.register_tool(
        name="srh_memory_write",
        toolset="mem_reflection_hermes",
        schema={
            "name": "srh_memory_write",
            "description": "Write a new structured memory with YAML frontmatter. Checks for conflicts. Specify zone to organize memories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "body": {"type": "string", "description": "Memory content (one short fact)"},
                    "scope": {"type": "string", "enum": ["user", "project"], "default": "user"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"], "default": "medium"},
                    "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                    "pinned": {"type": "boolean", "default": False},
                    "supersedes": {"type": "array", "items": {"type": "string"}, "default": []},
                    "zone": {"type": "string", "description": "Memory zone: core (identity/preferences), work (current focus), episode (session summaries), general (default), or project:<name>"},
                },
                "required": ["body"],
            },
        },
        handler=_tool_srh_memory_write,
        description="Write a structured memory",
        emoji="📝",
    )
    ctx.register_tool(
        name="srh_memory_delete",
        toolset="mem_reflection_hermes",
        schema={
            "name": "srh_memory_delete",
            "description": "Delete a memory by id from a scope.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Memory id"},
                    "scope": {"type": "string", "enum": ["user", "project"], "default": "user"},
                },
                "required": ["id"],
            },
        },
        handler=_tool_srh_memory_delete,
        description="Delete a memory",
        emoji="🗑️",
    )
    ctx.register_tool(
        name="srh_skill_search",
        toolset="mem_reflection_hermes",
        schema={
            "name": "srh_skill_search",
            "description": "Search skills by token overlap relevance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "default": 3},
                },
                "required": ["query"],
            },
        },
        handler=_tool_srh_skill_search,
        description="Search skills by relevance",
        emoji="🔧",
    )
    ctx.register_tool(
        name="srh_reflect_now",
        toolset="mem_reflection_hermes",
        schema={
            "name": "srh_reflect_now",
            "description": "Trigger or check status of reflection pipeline.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_tool_srh_reflect_now,
        description="Trigger reflection",
        emoji="🔍",
    )

    # Palace tools (zone-based memory navigation)
    ctx.register_tool(
        name="srh_palace_zones",
        toolset="mem_reflection_hermes",
        schema={
            "name": "srh_palace_zones",
            "description": "List all Memory Palace zones with memory counts. Use this to discover what zones exist before reading details.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_tool_srh_palace_zones,
        description="List memory zones",
        emoji="🏰",
    )
    ctx.register_tool(
        name="srh_palace_read_zone",
        toolset="mem_reflection_hermes",
        schema={
            "name": "srh_palace_read_zone",
            "description": "Load all memories from a specific zone. Returns cached zone summary if available, otherwise raw memory bodies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "zone": {"type": "string", "description": "Zone name (core, work, episode, general, or project:<name>)"},
                },
                "required": ["zone"],
            },
        },
        handler=_tool_srh_palace_read_zone,
        description="Read a memory zone",
        emoji="📂",
    )
    ctx.register_tool(
        name="srh_palace_recall",
        toolset="mem_reflection_hermes",
        schema={
            "name": "srh_palace_recall",
            "description": "Search memories by topic, optionally scoped to a zone. More focused than srh_memory_search — use this for palace navigation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "What to recall (e.g. 'editor preference', 'error handling convention')"},
                    "limit": {"type": "integer", "description": "Max results", "default": 5},
                    "zone": {"type": "string", "description": "Optional: restrict to a specific zone"},
                },
                "required": ["topic"],
            },
        },
        handler=_tool_srh_palace_recall,
        description="Recall by topic",
        emoji="🔎",
    )

    # Profile compilation tool (LLM-driven)
    ctx.register_tool(
        name="srh_compile_profile",
        toolset="mem_reflection_hermes",
        schema={
            "name": "srh_compile_profile",
            "description": "Compile all active memories into a structured profile document via LLM. Modes: 'profile' (profile.md), 'palace_index' (palace index), 'zone' (per-zone summaries).",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["profile", "palace_index", "zone"], "default": "profile", "description": "Compilation mode"},
                },
            },
        },
        handler=_tool_srh_compile_profile,
        description="Compile memories into profile",
        emoji="📋",
    )

    # Register hooks
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_llm_call", _pre_llm_call)

    # Register slash commands
    ctx.register_command(
        name="reflect",
        handler=lambda raw: _slash_reflect(raw),
        description="Trigger a full reflection on the current session",
        args_hint="",
    )
    ctx.register_command(
        name="pending-skills",
        handler=lambda raw: _slash_pending_skills(raw),
        description="Show pending skill candidates awaiting approval",
        args_hint="",
    )
    ctx.register_command(
        name="approve-skill",
        handler=lambda raw: _slash_approve_skill(raw),
        description="Approve a pending skill candidate by ID",
        args_hint="<pending_id>",
    )
    ctx.register_command(
        name="reject-skill",
        handler=lambda raw: _slash_reject_skill(raw),
        description="Reject a pending skill candidate by ID",
        args_hint="<pending_id> [reason]",
    )
    ctx.register_command(
        name="memories",
        handler=lambda raw: _slash_memories(raw),
        description="List active memories",
        args_hint="[query]",
    )
    ctx.register_command(
        name="skills",
        handler=lambda raw: _slash_skills(raw),
        description="List or search skills",
        args_hint="[query]",
    )
    ctx.register_command(
        name="compile-profile",
        handler=lambda raw: _slash_compile_profile(raw),
        description="Compile all memories into a structured profile via LLM",
        args_hint="[profile|palace_index|zone]",
    )

    logger.info("mem-reflection-hermes plugin registered")


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

def _slash_reflect(raw_args: str) -> str:
    return "🔍 Full reflection is now integrated with LLM. It runs automatically at session end, or you can trigger it via the srh_reflect_now tool."


def _slash_pending_skills(raw_args: str) -> str:
    """Show pending skill candidates for approval."""
    return _format_pending_skills_for_display()


def _slash_approve_skill(raw_args: str) -> str:
    """Approve a pending skill candidate by ID."""
    pending_id = raw_args.strip()
    if not pending_id:
        return "Usage: /approve-skill <pending_id>"
    result = _approve_skill(pending_id)
    if result and result.get("success"):
        return f"✅ Approved skill '{result['name']}' and saved to {result['path']}"
    return f"❌ Failed to approve: {result.get('error', 'Unknown error')}" if result else "❌ Failed to approve"


def _slash_reject_skill(raw_args: str) -> str:
    """Reject a pending skill candidate by ID."""
    parts = raw_args.strip().split(None, 1)
    if not parts:
        return "Usage: /reject-skill <pending_id> [reason]"
    pending_id = parts[0]
    reason = parts[1] if len(parts) > 1 else ""
    if _reject_skill(pending_id, reason):
        return f"❌ Rejected skill candidate {pending_id}"
    return f"❌ Failed to reject skill candidate {pending_id}"


def _slash_memories(raw_args: str) -> str:
    query = raw_args.strip()
    mem_store = _get_mem_store()
    if query:
        results = mem_store.search(query, k=10)
    else:
        results = mem_store.list_active()
    lines = [f"🧠 Active memories ({len(results)}):"]
    for m in results:
        pin = "📌" if m.frontmatter.pinned else "  "
        lines.append(f"{pin} [{m.id()}] {m.body[:120]}")
    return "\n".join(lines) if lines else "No memories found."


def _slash_skills(raw_args: str) -> str:
    query = raw_args.strip()
    skill_store = _get_skill_store()
    if query:
        skills = match_skills(skill_store.list(), query, k=10)
    else:
        skills = skill_store.list()
    lines = [f"🔧 Skills ({len(skills)}):"]
    for s in skills:
        lines.append(f"- {s.frontmatter.name}: {s.frontmatter.description}")
    return "\n".join(lines) if lines else "No skills found."


def _slash_compile_profile(raw_args: str) -> str:
    """Handle /compile-profile [mode] slash command."""
    mode = raw_args.strip() or "profile"
    if mode not in ("profile", "palace_index", "zone"):
        return f"⚠️ Unknown mode: {mode}. Use: profile, palace_index, or zone."
    # This requires ctx — can only work when called from a session with LLM access
    return (
        f"📋 Compile Profile command received (mode={mode}).\n"
        f"Use the srh_compile_profile tool with ctx access to execute, "
        f"or wait for session-end auto-compilation."
    )


# ---------------------------------------------------------------------------
# Embedding engine (ONNX Runtime — fast, lightweight)
# ---------------------------------------------------------------------------
# Lazy-loaded ONNX embedding components
# ---------------------------------------------------------------------------

_onnx_session: Optional[Any] = None
_onnx_tokenizer: Optional[Any] = None
_embed_model_lock = threading.Lock()

# LRU cache for embeddings: text_hash -> vector (max 500 entries)
_embed_cache: Dict[str, Any] = {}
_embed_cache_lock = threading.Lock()
_EMBED_CACHE_MAX = 500


def _embed_cache_key(text: str) -> str:
    """Hash text for cache key."""
    import hashlib
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _get_cached_embed(text: str) -> Optional[Any]:
    """Get cached embedding if available."""
    key = _embed_cache_key(text)
    with _embed_cache_lock:
        return _embed_cache.get(key)


def _set_cached_embed(text: str, vec: Any) -> None:
    """Cache embedding with LRU eviction."""
    key = _embed_cache_key(text)
    with _embed_cache_lock:
        if len(_embed_cache) >= _EMBED_CACHE_MAX:
            # Simple eviction: clear half the cache
            items = list(_embed_cache.items())
            _embed_cache.clear()
            _embed_cache.update(items[_EMBED_CACHE_MAX // 2:])
        _embed_cache[key] = vec


def _get_onnx_session() -> Tuple[Optional[Any], Optional[Any]]:
    """Lazy-load ONNX Runtime session and tokenizer.

    Uses all-MiniLM-L6-v2 in ONNX format for minimal memory footprint
    and fast inference.

    Model resolution priority:
    1. SRH_MODEL_DIR environment variable
    2. ~/.hermes/models/all-MiniLM-L6-v2-onnx/
    3. sentence-transformers fallback (auto-download)
    """
    global _onnx_session, _onnx_tokenizer
    if _onnx_session is not None and _onnx_tokenizer is not None:
        return _onnx_session, _onnx_tokenizer

    with _embed_model_lock:
        if _onnx_session is not None and _onnx_tokenizer is not None:
            return _onnx_session, _onnx_tokenizer

        # Resolve model directory
        env_model_dir = os.environ.get("SRH_MODEL_DIR")
        if env_model_dir:
            model_dir = Path(env_model_dir)
        else:
            model_dir = _hermes_home() / "models" / "all-MiniLM-L6-v2-onnx"
        model_path = model_dir / "model.onnx"

        # Fallback: try sentence-transformers if ONNX model not available
        if not model_path.exists():
            logger.warning("ONNX model not found at %s, falling back to sentence-transformers", model_path)
            return _get_st_model()

        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            _onnx_session = ort.InferenceSession(
                str(model_path),
                providers=["CPUExecutionProvider"],
            )
            _onnx_tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
            _onnx_tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            _onnx_tokenizer.enable_truncation(max_length=512)
            logger.info("Loaded ONNX embedding model from %s (lightweight tokenizer)", model_dir)
            return _onnx_session, _onnx_tokenizer
        except Exception as e:
            logger.warning("Failed to load ONNX model: %s", e)
            return _get_st_model()


def _get_st_model() -> Tuple[Optional[Any], Optional[Any]]:
    """Fallback to sentence-transformers if ONNX unavailable."""
    global _onnx_session, _onnx_tokenizer
    try:
        from sentence_transformers import SentenceTransformer
        _onnx_session = SentenceTransformer("all-MiniLM-L6-v2")
        _onnx_tokenizer = None  # ST has built-in tokenization
        logger.info("Loaded sentence-transformers fallback model")
        return _onnx_session, _onnx_tokenizer
    except Exception as e:
        logger.warning("Failed to load fallback embedding model: %s", e)
        return None, None


def _embed_texts(texts: List[str]) -> Optional[Any]:
    """Encode a list of texts into normalized embedding vectors."""
    if not texts:
        return None

    # Check cache for all texts
    cached_results = []
    uncached_texts = []
    uncached_indices = []
    for i, text in enumerate(texts):
        cached = _get_cached_embed(text)
        if cached is not None:
            cached_results.append((i, cached))
        else:
            uncached_texts.append(text)
            uncached_indices.append(i)

    # If all cached, return directly
    if not uncached_texts:
        return [vec for _, vec in sorted(cached_results, key=lambda x: x[0])]

    # Encode uncached texts
    session, tokenizer = _get_onnx_session()
    if session is None:
        return None

    embeddings = None

    # sentence-transformers fallback path
    if tokenizer is None and hasattr(session, "encode"):
        try:
            import numpy as np
            embeddings = session.encode(uncached_texts, convert_to_numpy=True, normalize_embeddings=True)
        except Exception as e:
            logger.debug("ST encoding failed: %s", e)
            return None
    else:
        # ONNX Runtime + tokenizers path
        try:
            import numpy as np

            # Tokenize
            encodings = tokenizer.encode_batch(uncached_texts)
            max_len = max(len(e.ids) for e in encodings)

            input_ids = np.array(
                [e.ids + [0] * (max_len - len(e.ids)) for e in encodings],
                dtype=np.int64,
            )
            attention_mask = np.array(
                [e.attention_mask + [0] * (max_len - len(e.attention_mask)) for e in encodings],
                dtype=np.int64,
            )
            token_type_ids = np.zeros_like(input_ids)

            # Run inference
            outputs = session.run(
                None,
                {
                    "input_ids": input_ids,
                    "attention_mask": attention_mask,
                    "token_type_ids": token_type_ids,
                },
            )
            last_hidden_state = outputs[0]  # (batch, seq_len, hidden_dim)

            # Mean pooling with attention mask
            mask_expanded = np.expand_dims(attention_mask, -1).astype(np.float32)
            sum_embeddings = np.sum(last_hidden_state * mask_expanded, axis=1)
            sum_mask = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
            embeddings = sum_embeddings / sum_mask

            # L2 normalize
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            embeddings = embeddings / norms

        except Exception as e:
            logger.debug("ONNX encoding failed: %s", e)
            return None

    # Cache new embeddings
    for text, vec in zip(uncached_texts, embeddings):
        _set_cached_embed(text, vec)

    # Merge cached + new results
    all_results = cached_results + list(zip(uncached_indices, embeddings))
    all_results.sort(key=lambda x: x[0])
    return [vec for _, vec in all_results]


def _embed_single(text: str) -> Optional[Any]:
    """Encode a single text into an embedding vector."""
    embs = _embed_texts([text])
    if embs is not None:
        return embs[0]
    return None


def _cosine_sim(a, b) -> float:
    """Cosine similarity between two normalized vectors."""
    try:
        import numpy as np
        return float(np.dot(a, b))
    except Exception:
        return 0.0


def _extract_keywords(text: str, top_k: int = 5) -> List[str]:
    """Extract distinctive keywords from text using TF-IDF-like heuristics."""
    tokens = _tokenise(text)
    if not tokens:
        return []
    # Filter out very common stopwords
    stops = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "must", "shall", "can", "need", "dare",
        "ought", "used", "to", "of", "in", "for", "on", "with", "at", "by",
        "from", "as", "into", "through", "during", "before", "after",
        "above", "below", "between", "under", "and", "but", "or", "yet",
        "so", "if", "because", "although", "though", "while", "where",
        "when", "that", "which", "who", "whom", "whose", "what", "this",
        "these", "those", "i", "you", "he", "she", "it", "we", "they",
        "me", "him", "her", "us", "them", "my", "your", "his", "its",
        "our", "their", "mine", "yours", "hers", "ours", "theirs",
        "myself", "yourself", "himself", "herself", "itself", "ourselves",
        "themselves", "what", "which", "who", "whom", "this", "that",
        "these", "those", "am", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "shall", "should", "may", "might", "can", "could",
        "must", "ought", "need", "dare", "used", "here", "there",
        "now", "then", "today", "tomorrow", "yesterday", "just", "only",
        "also", "even", "back", "after", "again", "further", "once",
        "about", "up", "out", "down", "off", "over", "under", "again",
    }
    # Count and score by rarity (rarer = higher score)
    tf = Counter(tokens)
    scored = []
    for t, c in tf.items():
        if t in stops or len(t) < 3:
            continue
        # Prefer longer, less frequent tokens
        score = c * len(t) / (1 + sum(1 for x in tokens if x == t))
        scored.append((score, t))
    scored.sort(reverse=True)
    seen = set()
    out = []
    for _, t in scored:
        if t not in seen:
            seen.add(t)
            out.append(t)
            if len(out) >= top_k:
                break
    return out


def _is_explicit_memory_intent(text: str) -> bool:
    """Detect if user explicitly wants to remember something."""
    lower = text.lower()
    markers = [
        "记住", "以后", "偏好", "总是", "remember", "always", "prefer",
        "不是", "不对", "错了", "don't", "wrong", "actually", "no,",
        "important", "note", "remind", "save this", "keep in mind",
        "我的", "我喜欢", "我讨厌", "i like", "i prefer", "i hate",
        "never", "always", "usually", "typically", "customarily",
    ]
    return any(m in lower for m in markers)


def _is_correction(text: str) -> bool:
    """Detect if user is correcting a previous statement."""
    lower = text.lower()
    markers = [
        "不对", "错了", "不是", "应该", "actually", "wrong", "correct",
        "instead", "rather", "meant", "mean", "更正", "纠正",
        "no,", "nope", "incorrect", "mistake", "fix", "修正",
    ]
    return any(m in lower for m in markers)


def _is_procedure(text: str) -> bool:
    """Detect if text describes a multi-step procedure."""
    lower = text.lower()
    markers = [
        "步骤", "流程", "首先", "然后", "最后", "step", "first", "then",
        "next", "finally", "procedure", "process", "workflow", "how to",
        "guide", "tutorial", "instruction", "1.", "2.", "3.",
    ]
    return any(m in lower for m in markers)


def _compute_novelty_score(new_text: str, existing_memories: List[LoadedMemory]) -> float:
    """Compute how novel a text is compared to existing memories (0-1, higher = more novel).

    Uses pre-computed memory embeddings from the store's embed_index when available
    for O(1) per-memory lookup instead of O(n) re-encoding.
    """
    if not existing_memories:
        return 1.0
    new_emb = _embed_single(new_text)
    if new_emb is None:
        # Fallback to TF-IDF
        return 1.0 - _tfidf_max_similarity(new_text, existing_memories)

    # Try to use store's embed_index for fast vector lookup
    store = _get_mem_store()
    max_sim = 0.0
    if store._embed_index is not None:
        vectors = store._embed_index.get("vectors", {})
        for m in existing_memories:
            m_emb = vectors.get(m.id())
            if m_emb is not None:
                sim = _cosine_sim(new_emb, m_emb)
                max_sim = max(max_sim, sim)
            else:
                # Fallback: encode on demand
                m_emb = _embed_single(m.body)
                if m_emb is not None:
                    sim = _cosine_sim(new_emb, m_emb)
                    max_sim = max(max_sim, sim)
    else:
        # No embed index: encode each memory on demand
        for m in existing_memories:
            m_emb = _embed_single(m.body)
            if m_emb is not None:
                sim = _cosine_sim(new_emb, m_emb)
                max_sim = max(max_sim, sim)

    # Scale: 0.9 similarity = 0.1 novelty, 0.0 similarity = 1.0 novelty
    novelty = max(0.0, 1.0 - max_sim)
    return novelty


def _find_conflicting_memory(new_text: str, existing: List[LoadedMemory], threshold: float = 0.75) -> Optional[Tuple[LoadedMemory, float]]:
    """Find a semantically similar but potentially conflicting memory.

    Uses pre-computed memory embeddings from the store's embed_index when available.
    """
    new_emb = _embed_single(new_text)
    if new_emb is None:
        return None

    store = _get_mem_store()
    best: Optional[Tuple[LoadedMemory, float]] = None

    if store._embed_index is not None:
        vectors = store._embed_index.get("vectors", {})
        for m in existing:
            m_emb = vectors.get(m.id())
            if m_emb is None:
                m_emb = _embed_single(m.body)
            if m_emb is not None:
                sim = _cosine_sim(new_emb, m_emb)
                if sim > threshold:
                    if best is None or sim > best[1]:
                        best = (m, sim)
    else:
        for m in existing:
            m_emb = _embed_single(m.body)
            if m_emb is not None:
                sim = _cosine_sim(new_emb, m_emb)
                if sim > threshold:
                    if best is None or sim > best[1]:
                        best = (m, sim)
    return best


def _extract_facts_from_turn(user_msg: str, assistant_msg: str) -> List[Dict[str, Any]]:
    """Extract potential fact statements from a conversation turn using heuristics."""
    facts = []
    combined = f"{user_msg} {assistant_msg}"

    # Heuristic 1: Explicit memory intent
    if _is_explicit_memory_intent(user_msg):
        # Extract the sentence containing the intent marker
        sentences = re.split(r'[。！？.!?\n]+', user_msg)
        for s in sentences:
            if _is_explicit_memory_intent(s):
                s = s.strip()
                if len(s) > 10:
                    facts.append({
                        "text": s,
                        "confidence": "high",
                        "rationale": "User explicitly requested to remember",
                        "source": "explicit_intent",
                    })

    # Heuristic 2: Corrections
    if _is_correction(user_msg):
        sentences = re.split(r'[。！？.!?\n]+', user_msg)
        for s in sentences:
            if _is_correction(s) and len(s) > 10:
                facts.append({
                    "text": s.strip(),
                    "confidence": "medium",
                    "rationale": "User corrected a previous statement",
                    "source": "correction",
                })

    # Heuristic 3: Preference statements
    pref_patterns = [
        r"(?:我|i)\s+(?:喜欢|prefer|like|want|想|要)\s+(.{5,80})",
        r"(?:我|i)\s+(?:不喜欢|hate|dislike|不想)\s+(.{5,80})",
        r"(?:我|i)\s+(?:总是|always|usually|never)\s+(.{5,80})",
        r"(?:用|use)\s+(.{3,40})\s+(?:因为|because)",
    ]
    for pat in pref_patterns:
        for m in re.finditer(pat, combined, re.IGNORECASE):
            text = m.group(0).strip()
            if len(text) > 10:
                facts.append({
                    "text": text,
                    "confidence": "medium",
                    "rationale": "Detected preference statement",
                    "source": "preference",
                })

    # Heuristic 4: Convention / config statements
    conv_patterns = [
        r"(?:配置|config|setting|设置)\s*[：:]\s*(.{5,80})",
        r"(?:默认|default)\s*[：:]\s*(.{5,80})",
        r"(?:约定|convention)\s*[：:]\s*(.{5,80})",
        r"(?:规则|rule)\s*[：:]\s*(.{5,80})",
    ]
    for pat in conv_patterns:
        for m in re.finditer(pat, combined, re.IGNORECASE):
            text = m.group(0).strip()
            if len(text) > 10:
                facts.append({
                    "text": text,
                    "confidence": "medium",
                    "rationale": "Detected configuration or convention",
                    "source": "convention",
                })

    # Deduplicate by text similarity
    deduped = []
    seen_texts = []
    for f in facts:
        is_dup = False
        for st in seen_texts:
            if _text_similarity(f["text"], st) > 0.8:
                is_dup = True
                break
        if not is_dup:
            seen_texts.append(f["text"])
            deduped.append(f)

    return deduped


def _text_similarity(a: str, b: str) -> float:
    """Quick text similarity using token overlap."""
    ta = set(_tokenise(a))
    tb = set(_tokenise(b))
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / max(len(ta), len(tb))


def _run_embedding_reflection(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run a full reflection using local embeddings + rule engine (zero LLM cost).

    This replaces the expensive LLM-based reflection with:
    1. Semantic novelty detection via embeddings
    2. Heuristic fact extraction from conversation
    3. Conflict detection via embedding similarity
    4. Conservative candidate generation
    """
    mem_store = _get_mem_store()
    skill_store = _get_skill_store()
    active_memories = mem_store.list_active()
    all_skills = skill_store.list()

    # Build transcript
    transcript = _format_messages_for_reflection(messages)
    if not transcript.strip():
        return {"summary": "Empty transcript", "accepted_memories": [], "skill_candidates": [], "conflicts": []}

    # Compute overall novelty of this session vs existing memories
    session_novelty = _compute_novelty_score(transcript, active_memories)
    logger.debug("Session novelty score: %.3f", session_novelty)

    # Extract potential facts from each turn
    memory_candidates = []
    conflicts = []

    # Process the full transcript as one unit for efficiency
    # (per-turn processing is done in micro-reflection)
    user_msgs = []
    assistant_msgs = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            content = " ".join(texts)
        if role == "user":
            user_msgs.append(content)
        elif role == "assistant":
            assistant_msgs.append(content)

    full_user = " ".join(user_msgs)
    full_assistant = " ".join(assistant_msgs)

    # Extract facts
    facts = _extract_facts_from_turn(full_user, full_assistant)

    for fact in facts:
        text = fact["text"]
        # Check novelty
        novelty = _compute_novelty_score(text, active_memories)
        if novelty < 0.3:
            logger.debug("Fact too similar to existing memory (novelty %.3f), skipping: %s", novelty, text[:60])
            continue

        # Check for conflicts
        conflict_mem = _find_conflicting_memory(text, active_memories)
        tags = _extract_keywords(text, top_k=3)

        if conflict_mem:
            mem, sim = conflict_mem
            # If very similar but user is correcting, mark as stale
            if _is_correction(full_user) and sim > 0.8:
                conflicts.append({
                    "with": mem.id(),
                    "kind": "stale",
                    "explain": f"User corrected previous information. Similarity: {sim:.2f}",
                    "options": ["keep_new", "keep_old"],
                })
                memory_candidates.append({
                    "fact": text,
                    "tags": tags,
                    "scope": "user",
                    "confidence": fact["confidence"],
                    "rationale": fact["rationale"],
                    "supersedes": [mem.id()],
                })
            else:
                # Just similar, not necessarily conflicting - skip to avoid duplication
                logger.debug("Similar to existing memory %s (%.3f), skipping", mem.id(), sim)
                continue
        else:
            memory_candidates.append({
                "fact": text,
                "tags": tags,
                "scope": "user",
                "confidence": fact["confidence"],
                "rationale": fact["rationale"],
                "supersedes": [],
            })

    # Also check if the overall session contains novel concepts not captured by explicit facts
    if session_novelty > 0.5 and len(memory_candidates) == 0:
        # Generate a summary memory from the session
        summary = _generate_session_summary(transcript)
        if summary and len(summary) > 20:
            tags = _extract_keywords(summary, top_k=3)
            memory_candidates.append({
                "fact": summary,
                "tags": tags,
                "scope": "user",
                "confidence": "low",
                "rationale": "Session contained novel concepts not matching existing memories",
                "supersedes": [],
            })

    # Skill detection: look for reusable procedures
    skill_candidates = []
    if _is_procedure(full_assistant) and len(full_assistant) > 200:
        # Check if similar skill already exists
        novel_skill = True
        emb_assistant = _embed_single(full_assistant)
        if emb_assistant is not None:
            for sk in all_skills:
                sk_emb = _embed_single(sk.body)
                if sk_emb is not None:
                    sim = _cosine_sim(emb_assistant, sk_emb)
                    if sim > 0.85:
                        novel_skill = False
                        break
        if novel_skill:
            name = _generate_skill_name(full_assistant)
            skill_candidates.append({
                "name": name,
                "description": f"Procedure extracted from session: {summary[:80] if summary else 'multi-step workflow'}",
                "triggers": tags[:3] if tags else ["procedure"],
                "body": f"## {name}\n\n{full_assistant[:800]}",
                "rationale": "Assistant provided a multi-step procedure that may be reusable",
                "confidence": "low",
            })

    # Store memory candidates
    accepted_memories = []
    for cand in memory_candidates:
        try:
            fm = MemoryFrontmatter.new(
                source="reflection",
                confidence=cand.get("confidence", "medium"),
                tags=cand.get("tags", []),
            )
            fm.supersedes = cand.get("supersedes", [])
            scope = cand.get("scope", "user")
            body = cand["fact"]
            # Final conflict check
            conflict = mem_store.check_conflict(body)
            if conflict:
                existing_id, score = conflict
                logger.info("Embedding reflection: memory conflicts with %s (%.2f), skipping", existing_id, score)
                continue
            path = mem_store.put(scope, fm, body)
            accepted_memories.append({"id": fm.id, "body": body, "path": str(path)})
        except Exception as e:
            logger.warning("Failed to store embedding reflection memory: %s", e)

    # Save skill candidates for approval
    if skill_candidates:
        logger.info("Embedding reflection produced %d skill candidates (manual approval required)", len(skill_candidates))
        _save_pending_skill_candidates(skill_candidates)

    # Build summary
    summary = f"Session novelty: {session_novelty:.2f}. Extracted {len(facts)} facts, accepted {len(accepted_memories)} memories, {len(skill_candidates)} skills pending, {len(conflicts)} conflicts."

    _append_reflect_log({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "embedding",
        "summary": summary,
        "skill_candidates": len(skill_candidates),
        "memory_candidates": len(memory_candidates),
        "accepted_memories": len(accepted_memories),
        "conflicts": len(conflicts),
        "novelty": session_novelty,
    })

    logger.info(
        "Embedding reflection complete: %d memories accepted, %d skills pending, %d conflicts",
        len(accepted_memories), len(skill_candidates), len(conflicts),
    )

    return {
        "summary": summary,
        "accepted_memories": accepted_memories,
        "skill_candidates": skill_candidates,
        "conflicts": conflicts,
    }


def _run_embedding_micro_reflection(user_msg: str, assistant_msg: str) -> Optional[Dict[str, Any]]:
    """Run a micro-reflection using local embeddings (zero LLM cost).

    Much faster than LLM-based micro-reflection (~50ms vs ~2000ms).
    """
    mem_store = _get_mem_store()
    active_memories = mem_store.list_active()

    combined = f"{user_msg} {assistant_msg}"

    # Extract facts first - if user has explicit intent, always process
    facts = _extract_facts_from_turn(user_msg, assistant_msg)
    has_explicit_intent = _is_explicit_memory_intent(user_msg)

    # Quick novelty check - but skip if user explicitly wants to remember
    novelty = _compute_novelty_score(combined, active_memories)
    if not has_explicit_intent and novelty < 0.25:
        logger.debug("Micro-reflection: turn too similar to existing memories (%.3f), skipping", novelty)
        return None

    if not facts:
        # Even without heuristic facts, if novelty is high and user said something substantive,
        # create a generic memory
        if novelty > 0.6 and len(user_msg) > 20:
            facts = [{
                "text": user_msg[:200],
                "confidence": "low",
                "rationale": "Novel user message with no explicit intent markers",
                "source": "novelty",
            }]
        else:
            return None

    # Only take the highest-confidence fact
    facts.sort(key=lambda f: 0 if f["confidence"] == "high" else (1 if f["confidence"] == "medium" else 2))
    best = facts[0]

    # Check conflict
    conflict_mem = _find_conflicting_memory(best["text"], active_memories)
    tags = _extract_keywords(best["text"], top_k=3)

    supersedes = []
    if conflict_mem:
        mem, sim = conflict_mem
        if _is_correction(user_msg) and sim > 0.7:
            supersedes = [mem.id()]
        elif has_explicit_intent and sim > 0.85:
            # Very similar and user explicitly stated - likely an update
            supersedes = [mem.id()]
        else:
            logger.debug("Micro-reflection: similar to %s (%.3f), skipping", mem.id(), sim)
            return None

    try:
        fm = MemoryFrontmatter.new(
            source="micro_reflection",
            confidence=best["confidence"],
            tags=tags,
        )
        fm.supersedes = supersedes
        path = mem_store.put("user", fm, best["text"])
        accepted = {"id": fm.id, "body": best["text"], "path": str(path)}

        _append_reflect_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "embedding_micro",
            "summary": f"Micro-reflection accepted: {best['text'][:60]}",
            "accepted_memory": accepted,
            "novelty": novelty,
        })

        return {
            "summary": f"Detected {best['source']}: {best['text'][:60]}",
            "memory_candidates": [{"fact": best["text"], "tags": tags, "scope": "user", "confidence": best["confidence"]}],
            "skill_candidates": [],
            "conflicts": [],
        }
    except Exception as e:
        logger.debug("Micro-reflection storage failed: %s", e)
        return None


def _generate_session_summary(transcript: str) -> str:
    """Generate a brief summary of the session from the transcript.

    Uses simple heuristics (first/last user messages) rather than LLM.
    """
    lines = [l for l in transcript.split("\n") if l.strip() and not l.startswith("[")]
    if not lines:
        return ""
    # Take the first substantial line
    for line in lines:
        clean = line.strip()
        if len(clean) > 20:
            return clean[:200]
    return lines[0][:200] if lines else ""


def _generate_skill_name(text: str) -> str:
    """Generate a kebab-case skill name from text heuristics."""
    keywords = _extract_keywords(text, top_k=3)
    if keywords:
        return "-".join(keywords[:3])
    # Fallback: use first few words
    words = re.findall(r"[a-zA-Z]+", text.lower())
    if words:
        return "-".join(words[:3])
    return "extracted-procedure"


# ---------------------------------------------------------------------------
# LLM-powered reflection (kept as fallback for hybrid mode)
# ---------------------------------------------------------------------------

def _build_reflect_schema() -> Dict[str, Any]:
    """Build JSON schema for reflection structured output."""
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "skill_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "triggers": {"type": "array", "items": {"type": "string"}},
                        "body": {"type": "string"},
                        "rationale": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                    "required": ["name", "description", "triggers", "body", "rationale", "confidence"],
                },
            },
            "memory_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "fact": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "scope": {"type": "string", "enum": ["user", "project"]},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                        "rationale": {"type": "string"},
                        "supersedes": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["fact", "tags", "scope", "confidence", "rationale"],
                },
            },
            "conflicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "with": {"type": "string"},
                        "kind": {"type": "string", "enum": ["contradiction", "redundancy", "scope_overlap", "stale"]},
                        "explain": {"type": "string"},
                        "options": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["with", "kind", "explain", "options"],
                },
            },
        },
        "required": ["summary", "skill_candidates", "memory_candidates", "conflicts"],
    }


def _format_messages_for_reflection(messages: List[Dict[str, Any]]) -> str:
    """Format message list into a transcript string for reflection."""
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Extract text from multi-modal content
            texts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text", ""))
            content = "\n".join(texts)
        lines.append(f"[{role}] {content}")
    return "\n\n".join(lines)


def _format_inventory() -> str:
    """Format current memory and skill inventory for reflection context."""
    mem_store = _get_mem_store()
    skill_store = _get_skill_store()
    lines = ["=== Current Memory Inventory ==="]
    for m in mem_store.list_active():
        lines.append(f"- [{m.id()}] {m.body[:120]} (tags: {m.frontmatter.tags}, confidence: {m.frontmatter.confidence})")
    lines.append("")
    lines.append("=== Current Skill Inventory ===")
    for s in skill_store.list():
        lines.append(f"- {s.frontmatter.name}: {s.frontmatter.description} (triggers: {s.frontmatter.triggers})")
    return "\n".join(lines)


def _run_full_reflection(ctx, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run a full reflection. Uses embedding-based reflection by default;
    falls back to LLM only if reflection_mode is 'llm' or 'hybrid'."""
    mode = _reflection_mode()

    # Default to embedding-based (local, zero cost)
    if mode in ("embedding", "local"):
        return _run_embedding_reflection(messages)

    # Hybrid: try embedding first, if no candidates found, try LLM
    if mode == "hybrid":
        emb_result = _run_embedding_reflection(messages)
        if emb_result.get("accepted_memories") or emb_result.get("skill_candidates"):
            return emb_result
        logger.info("Hybrid mode: embedding found no candidates, trying LLM fallback")
        # Fall through to LLM

    # LLM mode (expensive, kept for compatibility)
    if not hasattr(ctx, "llm"):
        logger.warning("No ctx.llm available for full reflection")
        return {"error": "No LLM available"}

    transcript = _format_messages_for_reflection(messages)
    inventory = _format_inventory()

    instructions = (
        "Analyze the following conversation transcript and current agent inventory. "
        "Identify skill candidates, memory candidates, and conflicts. "
        "Be conservative — only propose high-quality candidates."
    )

    inputs = [
        {"type": "text", "text": f"=== TRANSCRIPT ===\n\n{transcript}\n\n{inventory}"},
    ]

    try:
        result = ctx.llm.complete_structured(
            instructions=instructions,
            input=inputs,
            json_schema=_build_reflect_schema(),
            json_mode=True,
            system_prompt=_FULL_REFLECT_SYSTEM,
            purpose="full_reflection",
            max_tokens=4096,
        )
    except Exception as e:
        logger.warning("LLM reflection call failed: %s", e)
        return {"error": str(e)}

    parsed = result.parsed if result else None
    if not parsed:
        logger.warning("Reflection produced no parsed output")
        return {"error": "No parsed output"}

    # Log the reflection outcome
    _append_reflect_log({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "full_llm",
        "summary": parsed.get("summary", ""),
        "skill_candidates": len(parsed.get("skill_candidates", [])),
        "memory_candidates": len(parsed.get("memory_candidates", [])),
        "conflicts": len(parsed.get("conflicts", [])),
        "raw": result.text,
    })

    # Store memory candidates automatically (they're conservative)
    mem_store = _get_mem_store()
    accepted_memories = []
    for cand in parsed.get("memory_candidates", []):
        try:
            fm = MemoryFrontmatter.new(
                source="reflection",
                confidence=cand.get("confidence", "medium"),
                tags=cand.get("tags", []),
            )
            fm.supersedes = cand.get("supersedes", [])
            scope = cand.get("scope", "user")
            body = cand["fact"]
            # Conflict check
            conflict = mem_store.check_conflict(body)
            if conflict:
                existing_id, score = conflict
                logger.info("Reflection memory candidate conflicts with %s (%.2f), skipping", existing_id, score)
                continue
            path = mem_store.put(scope, fm, body)
            accepted_memories.append({"id": fm.id, "body": body, "path": str(path)})
        except Exception as e:
            logger.warning("Failed to store memory candidate: %s", e)

    # Log skill candidates (require manual approval)
    skill_candidates = parsed.get("skill_candidates", [])
    if skill_candidates:
        logger.info("Reflection produced %d skill candidates (manual approval required)", len(skill_candidates))
        # Save pending skill candidates for user approval
        _save_pending_skill_candidates(skill_candidates)

    logger.info(
        "Full reflection complete: %d memories accepted, %d skills pending approval, %d conflicts noted",
        len(accepted_memories), len(skill_candidates), len(parsed.get("conflicts", [])),
    )

    return {
        "summary": parsed.get("summary", ""),
        "accepted_memories": accepted_memories,
        "skill_candidates": skill_candidates,
        "conflicts": parsed.get("conflicts", []),
    }


def _run_micro_reflection(ctx, user_msg: str, assistant_msg: str) -> Optional[Dict[str, Any]]:
    """Run a micro-reflection. Uses embedding-based by default; falls back to LLM only in 'llm' mode."""
    mode = _reflection_mode()

    if mode in ("embedding", "local", "hybrid"):
        return _run_embedding_micro_reflection(user_msg, assistant_msg)

    # LLM mode (expensive)
    if not hasattr(ctx, "llm"):
        return None

    instructions = (
        "You just observed ONE turn of conversation. "
        "Decide if anything is worth persisting as a memory or skill."
    )

    inputs = [
        {"type": "text", "text": f"[user] {user_msg}\n\n[assistant] {assistant_msg}"},
    ]

    try:
        result = ctx.llm.complete_structured(
            instructions=instructions,
            input=inputs,
            json_schema=_build_reflect_schema(),
            json_mode=True,
            system_prompt=_MICRO_REFLECT_SYSTEM,
            purpose="micro_reflection",
            max_tokens=2048,
        )
    except Exception as e:
        logger.debug("Micro-reflection LLM call failed: %s", e)
        return None

    parsed = result.parsed if result else None
    if not parsed:
        return None

    # Store at most 1 memory from micro-reflection (auto-accepted for micro)
    mem_store = _get_mem_store()
    accepted = None
    for cand in parsed.get("memory_candidates", [])[:1]:
        try:
            fm = MemoryFrontmatter.new(
                source="micro_reflection",
                confidence=cand.get("confidence", "low"),
                tags=cand.get("tags", []),
            )
            fm.supersedes = cand.get("supersedes", [])
            scope = cand.get("scope", "user")
            body = cand["fact"]
            conflict = mem_store.check_conflict(body)
            if conflict:
                continue
            path = mem_store.put(scope, fm, body)
            accepted = {"id": fm.id, "body": body, "path": str(path)}
        except Exception:
            pass

    if accepted:
        _append_reflect_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "micro_llm",
            "summary": parsed.get("summary", ""),
            "accepted_memory": accepted,
        })

    return parsed


# ---------------------------------------------------------------------------
# Pending skill candidate approval system
# ---------------------------------------------------------------------------

PENDING_SKILLS_PATH = _plugin_data_dir() / "pending-skills.json"


def _save_pending_skill_candidates(candidates: List[Dict[str, Any]]) -> None:
    """Save skill candidates to pending approval file."""
    try:
        PENDING_SKILLS_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if PENDING_SKILLS_PATH.exists():
            with open(PENDING_SKILLS_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        # Add timestamp and unique id to each candidate
        for cand in candidates:
            cand["_pending_id"] = f"pending_{uuid.uuid4().hex[:12]}"
            cand["_submitted_at"] = datetime.now(timezone.utc).isoformat()
            cand["_status"] = "pending"
        existing.extend(candidates)
        with open(PENDING_SKILLS_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Failed to save pending skill candidates: %s", e)


def _load_pending_skill_candidates() -> List[Dict[str, Any]]:
    """Load all pending skill candidates."""
    try:
        if not PENDING_SKILLS_PATH.exists():
            return []
        with open(PENDING_SKILLS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _update_pending_skill_status(pending_id: str, status: str, reason: str = "") -> bool:
    """Update the status of a pending skill candidate."""
    try:
        candidates = _load_pending_skill_candidates()
        for cand in candidates:
            if cand.get("_pending_id") == pending_id:
                cand["_status"] = status
                cand["_resolved_at"] = datetime.now(timezone.utc).isoformat()
                cand["_resolve_reason"] = reason
                with open(PENDING_SKILLS_PATH, "w", encoding="utf-8") as f:
                    json.dump(candidates, f, ensure_ascii=False, indent=2)
                return True
        return False
    except Exception as e:
        logger.warning("Failed to update pending skill status: %s", e)
        return False


def _approve_skill(pending_id: str) -> Optional[Dict[str, Any]]:
    """Approve a pending skill candidate and write it to the skill store."""
    candidates = _load_pending_skill_candidates()
    for cand in candidates:
        if cand.get("_pending_id") == pending_id:
            if cand.get("_status") != "pending":
                return {"error": f"Skill already {cand['_status']}"}
            try:
                # Write skill to user skills directory
                skill_name = cand["name"]
                skill_dir = _user_skills_dir() / skill_name
                skill_dir.mkdir(parents=True, exist_ok=True)

                fm_data = {
                    "name": skill_name,
                    "description": cand.get("description", ""),
                    "triggers": cand.get("triggers", []),
                    "version": "1.0.0",
                    "license": "MIT",
                }
                body = cand.get("body", "")
                skill_md = _serialize_frontmatter(fm_data, body)
                (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

                _update_pending_skill_status(pending_id, "approved", "User approved via UI")
                return {
                    "success": True,
                    "name": skill_name,
                    "path": str(skill_dir),
                }
            except Exception as e:
                logger.warning("Failed to approve skill %s: %s", pending_id, e)
                return {"error": str(e)}
    return {"error": "Pending skill not found"}


def _reject_skill(pending_id: str, reason: str = "") -> bool:
    """Reject a pending skill candidate."""
    return _update_pending_skill_status(pending_id, "rejected", reason or "User rejected via UI")


def _format_pending_skills_for_display() -> str:
    """Format pending skills for TUI/gateway display."""
    candidates = [c for c in _load_pending_skill_candidates() if c.get("_status") == "pending"]
    if not candidates:
        return "No pending skill candidates."

    lines = [f"🔧 Pending Skill Candidates ({len(candidates)}):", ""]
    for i, cand in enumerate(candidates, 1):
        lines.append(f"{i}. {cand['name']}")
        lines.append(f"   Description: {cand.get('description', 'N/A')}")
        lines.append(f"   Triggers: {', '.join(cand.get('triggers', []))}")
        lines.append(f"   Confidence: {cand.get('confidence', 'medium')}")
        lines.append(f"   Rationale: {cand.get('rationale', 'N/A')}")
        lines.append(f"   Pending ID: {cand['_pending_id']}")
        lines.append("")
    lines.append("Use /approve-skill <pending_id> or /reject-skill <pending_id> to act on these.")
    return "\n".join(lines)
