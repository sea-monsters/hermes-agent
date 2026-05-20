#!/usr/bin/env python3
"""
mem-reflection-hermes latency benchmark — POST-OPTIMIZATION (P0-P2)

Validates:
  P0-1: palace write-on-change (warm context skips write)
  P0-2: delete O(1) via id→path index
  P1-1: token byte estimation (fast path)
  P1-2: stat async flush (non-blocking)
  P2-1: event-driven index rebuild
  P2-2: async memory write (non-blocking)
"""
from __future__ import annotations

import json, os, statistics, sys, tempfile, time, threading
from pathlib import Path
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
TMPDIR = Path(tempfile.mkdtemp(prefix="hermes_bench_"))
(TMPDIR / "memory" / "memories").mkdir(parents=True, exist_ok=True)
(TMPDIR / "memory" / "skills").mkdir(parents=True, exist_ok=True)
(TMPDIR / "memory" / "zone-cache").mkdir(parents=True, exist_ok=True)
(TMPDIR / "plugins" / "data").mkdir(parents=True, exist_ok=True)

os.environ["HERMES_HOME"] = str(TMPDIR)

config = {
    "plugins": {
        "mem_reflection_hermes": {
            "palace_mode": True,
            "profile_mode": False,
            "palace_instructions": True,
            "active_memory_index_cap": 50,
            "skill_index_cap": 50,
            "relevant_memory_cap": 3,
            "triggered_skill_cap": 3,
            "max_context_token_preference": 6000,
            "micro_reflection": {"enabled": False},
            "embeddings": False,
        }
    }
}
import yaml
(TMPDIR / "config.yaml").write_text(yaml.dump(config), encoding="utf-8")

plugin_dir = str(Path.home() / ".hermes" / "plugins" / "mem-reflection-hermes")
sys.path.insert(0, plugin_dir)
import importlib
import __init__ as plugin

# Disable embedding path
plugin._onnx_session = "DISABLED"
plugin._onnx_tokenizer = "DISABLED"
plugin._mem_store = None
plugin._skill_store = None
plugin._cached_config = None
plugin._cached_config_mtime = 0.0
plugin.MemoryStore._ensure_embed = lambda self: False
plugin.MemoryStore._embed_search = lambda self, q, k: None

# Verify
print(f"Config: embeddings={plugin._embeddings_enabled()} palace={plugin._palace_mode_enabled()}")

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
ZONES = ["core", "work", "episode", "general", None]
TOPICS = [
    "python coding preferences", "testing framework choice", "deployment pipeline config",
    "database schema design", "API authentication", "error handling patterns",
    "logging conventions", "project naming standards", "code review checklist",
    "performance optimization tips", "memory management", "git workflow",
    "CI/CD setup", "containerization strategy", "monitoring dashboards",
    "security best practices", "documentation style", "dependency management",
    "refactoring patterns", "team communication protocols",
]
SKILLS_DATA = [
    ("python-debugging", "Debug python code with pdb", ["python", "debug"]),
    ("git-workflow", "Git branch/commit/PR", ["git", "commit", "pr"]),
    ("code-review", "Review PRs", ["review", "pr", "checklist"]),
    ("database-migration", "Schema migration alembic", ["db", "migrate"]),
    ("docker-setup", "Docker container build", ["docker", "container"]),
    ("api-design", "REST API design", ["rest", "api"]),
    ("testing-guide", "Unit/integration tests", ["test", "pytest"]),
    ("ci-cd-pipeline", "GitHub Actions CI/CD", ["ci", "cd", "github"]),
    ("performance-tuning", "CPU/memory profiling", ["perf", "profile"]),
    ("security-audit", "Security scanning", ["security", "scan"]),
]
QUERIES = [
    "python testing framework",
    "how to debug performance issues",
    "security vulnerability scanning",
]

def create_test_data():
    mem_store = plugin._get_mem_store()
    skill_store = plugin._get_skill_store()
    for i in range(50):
        zone = ZONES[i % len(ZONES)]
        topic = TOPICS[i % len(TOPICS)]
        fm = plugin.MemoryFrontmatter.new(source="benchmark", confidence="medium",
            tags=[zone or "general", topic.replace(" ", "-")])
        fm.zone = zone
        body = f"[bench-{i:03d}] {topic}: detail #{i}. "
        body += "Extra context for realistic token counts. "
        body += f"Memory {i} about {topic} in zone {zone or 'general'}."
        try:
            mem_store.put("user", fm, body)
        except ValueError:
            pass
    skills_dir = TMPDIR / "memory" / "skills"
    for name, desc, triggers in SKILLS_DATA:
        content = f"---\nname: {name}\ndescription: {desc}\ntriggers: {triggers}\nalways_active: false\n---\n\n# {name}\n\n{desc}.\n"
        (skills_dir / f"{name}.md").write_text(content, encoding="utf-8")
    # Wait for async writes to complete
    time.sleep(1.0)
    plugin._skill_store = None
    plugin._mem_store = None
    plugin._cached_config = None
    return mem_store, skill_store

create_test_data()
print("Test data ready.")

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
def bench(fn, *args, warmup=1, iterations=5, **kw):
    for _ in range(warmup): fn(*args, **kw)
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn(*args, **kw)
        times.append((time.perf_counter() - t0) * 1000)
    s = sorted(times); n = len(times)
    return {"mean": statistics.mean(times), "median": statistics.median(times),
            "stdev": statistics.stdev(times) if n>1 else 0, "p95": s[min(int(n*.95),n-1)],
            "p99": s[min(int(n*.99),n-1)], "min": min(times), "max": max(times), "n": n}

def fmt(ms):
    if ms < 0.001: return f"{ms*1e6:.1f}ns"
    if ms < 1: return f"{ms*1000:.1f}µs"
    if ms < 1000: return f"{ms:.2f}ms"
    return f"{ms/1000:.2f}s"

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    results = []

    # Fresh stores
    plugin._mem_store = None; plugin._skill_store = None
    plugin._cached_config = None; plugin._cached_config_mtime = 0.0
    ms = plugin._get_mem_store(); ss = plugin._get_skill_store()
    ms._ensure_cache(); _ = ss.list()

    # -- Init --
    def do_init():
        plugin._mem_store = None; plugin._skill_store = None
        plugin._cached_config = None; plugin._cached_config_mtime = 0.0
        m = plugin._get_mem_store(); m._ensure_cache()
        s = plugin._get_skill_store(); _ = s.list()
        return m, s
    stats = bench(do_init, iterations=5, warmup=1)
    results.append(("🔌 Init", "Store construction + cache", stats))

    # -- Context Block (palace, cold=dirty flag set) --
    def ctx_cold():
        plugin._cached_config = None; plugin._cached_config_mtime = 0.0
        ms._index_dirty = True  # force dirty to test write path
        return plugin._build_context_block(query=QUERIES[0])
    stats = bench(ctx_cold, iterations=5, warmup=1)
    results.append(("📦 Context (cold/dirty)", "Palace: full rebuild + write-on-change", stats))

    # -- Context Block (palace, warm=clean) --
    def ctx_warm():
        plugin._cached_config = None; plugin._cached_config_mtime = 0.0
        return plugin._build_context_block(query=QUERIES[0])
    stats = bench(ctx_warm, iterations=5, warmup=1)
    results.append(("📦 Context (warm/P0-1)", "Palace: skip write (hash unchanged) ⚡", stats))

    # -- Legacy mode --
    config["plugins"]["mem_reflection_hermes"]["palace_mode"] = False
    (TMPDIR / "config.yaml").write_text(yaml.dump(config), encoding="utf-8")
    def ctx_legacy():
        plugin._cached_config = None; plugin._cached_config_mtime = 0.0
        return plugin._build_context_block(query=QUERIES[0])
    stats = bench(ctx_legacy, iterations=5, warmup=1)
    results.append(("📦 Context (legacy)", "TF-IDF + triggered skills", stats))
    config["plugins"]["mem_reflection_hermes"]["palace_mode"] = True
    (TMPDIR / "config.yaml").write_text(yaml.dump(config), encoding="utf-8")

    # -- Profile mode --
    config["plugins"]["mem_reflection_hermes"]["profile_mode"] = True
    (TMPDIR / "plugins" / "data" / "profile.md").write_text("# Profile\nTest.", encoding="utf-8")
    (TMPDIR / "config.yaml").write_text(yaml.dump(config), encoding="utf-8")
    def ctx_prof():
        plugin._cached_config = None; plugin._cached_config_mtime = 0.0
        return plugin._build_context_block(query=QUERIES[0])
    stats = bench(ctx_prof, iterations=5, warmup=1)
    results.append(("📦 Context (profile)", "Read compiled profile", stats))
    config["plugins"]["mem_reflection_hermes"]["profile_mode"] = False
    (TMPDIR / "config.yaml").write_text(yaml.dump(config), encoding="utf-8")

    # -- Memory Search --
    for qi, q in enumerate(QUERIES):
        ms._doc_tokens = None
        stats = bench(lambda q=q: ms.search(q, k=5), iterations=10, warmup=2)
        results.append((f"🔍 Search #{qi+1}", f'"{q[:40]}"', stats))

    # -- P2-2: Async Memory Write (agent perceived latency) --
    def bench_put_async():
        fm = plugin.MemoryFrontmatter.new(source="bench_async", confidence="medium", tags=["bench"])
        fm.zone = "general"
        body = "Async write benchmark memory."
        t0 = time.perf_counter()
        path = ms.put("user", fm, body)
        latency = (time.perf_counter() - t0) * 1000
        time.sleep(0.01)  # Let async writer flush
        ms.delete("user", fm.id)
        return latency
    timings = [bench_put_async() for _ in range(20)][5:]
    s = sorted(timings); n = len(timings)
    stats = {"mean": statistics.mean(timings), "median": statistics.median(timings),
             "stdev": statistics.stdev(timings), "p95": s[min(int(n*.95),n-1)],
             "p99": s[min(int(n*.99),n-1)], "min": min(timings), "max": max(timings), "n": n}
    results.append(("📝 Put (async/P2-2)", "Agent perceived latency (no disk wait) ⚡", stats))

    # -- P0-2: Delete O(1) --
    def bench_delete():
        fm = plugin.MemoryFrontmatter.new(source="bench_del", confidence="low", tags=["bench"])
        body = "Delete benchmark."
        ms.put("user", fm, body)
        time.sleep(0.02)
        t0 = time.perf_counter()
        ms.delete("user", fm.id)
        return (time.perf_counter() - t0) * 1000
    timings = [bench_delete() for _ in range(20)][5:]
    s = sorted(timings); n = len(timings)
    stats = {"mean": statistics.mean(timings), "median": statistics.median(timings),
             "stdev": statistics.stdev(timings), "p95": s[min(int(n*.95),n-1)],
             "p99": s[min(int(n*.99),n-1)], "min": min(timings), "max": max(timings), "n": n}
    results.append(("🗑️ Delete (O(1)/P0-2)", "id→path index lookup ⚡", stats))

    # -- P1-1: Token Estimate --
    text_2k = " ".join(TOPICS) * 10
    stats = bench(lambda: plugin._estimate_tokens(text_2k), iterations=50, warmup=10)
    results.append(("🧮 Token Est (P1-1)", "Fast bytes-based ~2000 chars ⚡", stats))

    # -- P1-2: Stat Async Flush --
    def bench_stat_async():
        entries = [(f"b-{i}", "loaded") for i in range(10)]
        t0 = time.perf_counter()
        plugin._batch_record_stats(entries)
        return (time.perf_counter() - t0) * 1000
    stats = bench(bench_stat_async, iterations=20, warmup=5)
    results.append(("📊 Stat Flush (P1-2)", "Non-blocking queue submit ⚡", stats))

    # -- Misc --
    stats = bench(lambda: plugin.match_skills(ss.list(), QUERIES[0], k=3), iterations=10, warmup=2)
    results.append(("🔧 Skill Search", "Token overlap", stats))

    stats = bench(lambda: plugin._tool_srh_palace_recall({"topic": QUERIES[0], "limit": 5}),
                  iterations=10, warmup=2)
    results.append(("🔎 Palace Recall", "TF-IDF + zone", stats))

    stats = bench(lambda: plugin._parse_frontmatter(
        "---\nid: t\ncreated: 2025-01-01T00:00:00Z\nsource: t\nconfidence: m\ntags: []\n---\n\nBody."),
        iterations=20, warmup=5)
    results.append(("📋 Parse FM", "YAML parse", stats))

    # -- Print --
    print("\n" + "="*95)
    print(f"{'POST-OPTIMIZATION BENCHMARK (P0-P2)':^95}")
    print(f"Memories: 50 | Skills: 10 | Palace mode | Async I/O: ON")
    print("="*95)
    print(f"\n{'Step':<34} {'Mean':>9} {'Median':>9} {'P95':>9} {'Min':>9} {'Max':>9}  Note")
    print("-"*95)
    total = 0
    for name, note, s in results:
        flag = " ⚡" if "⚡" in note else ""
        print(f"{name:<34} {fmt(s['mean']):>9} {fmt(s['median']):>9} {fmt(s['p95']):>9} {fmt(s['min']):>9} {fmt(s['max']):>9} {flag}")
        total += s['mean']
    print("-"*95)
    print(f"{'SUM':<34} {fmt(total):>9}")
    print()

    # -- Comparison with baseline --
    print("="*95)
    print("BEFORE vs AFTER COMPARISON")
    print("="*95)
    baseline = {
        "Context (palace)": 1.74, "Context (legacy)": 2.39,
        "Search": 1.08, "Put+Delete": 11.69, "Delete": 10.68,
        "Token Est": 2.96, "Stat Flush": 0.206,
    }
    after = {}
    for name, _, s in results:
        if "Context (cold" in name: after["Context (palace, cold)"] = s["mean"]
        if "Context (warm" in name: after["Context (palace, warm)"] = s["mean"]
        if "Context (legacy" in name: after["Context (legacy)"] = s["mean"]
        if "Search #1" in name: after["Search"] = s["mean"]
        if "Put (async" in name: after["Put (async)"] = s["mean"]
        if "Delete (O(1)" in name: after["Delete"] = s["mean"]
        if "Token Est" in name: after["Token Est"] = s["mean"]
        if "Stat Flush" in name: after["Stat Flush"] = s["mean"]

    comparisons = [
        ("Context (palace, warm) ⚡", baseline["Context (palace)"], after.get("Context (palace, warm)", 0)),
        ("Context (palace, cold)", baseline["Context (palace)"], after.get("Context (palace, cold)", 0)),
        ("Context (legacy)", baseline["Context (legacy)"], after.get("Context (legacy)", 0)),
        ("Search", baseline["Search"], after.get("Search", 0)),
        ("Put (agent perceived) ⚡", baseline["Put+Delete"], after.get("Put (async)", 0)),
        ("Delete ⚡", baseline["Delete"], after.get("Delete", 0)),
        ("Token Estimate ⚡", baseline["Token Est"], after.get("Token Est", 0)),
        ("Stat Flush ⚡", baseline["Stat Flush"], after.get("Stat Flush", 0)),
    ]
    print(f"\n{'Step':<34} {'Before':>9} {'After':>9} {'Delta':>9}  {'Change':>10}")
    print("-"*75)
    for name, before, aft in comparisons:
        if aft == 0: continue
        delta = aft - before
        pct = (delta / before * 100) if before else 0
        sign = "↓" if delta < 0 else "↑"
        print(f"{name:<34} {fmt(before):>9} {fmt(aft):>9} {fmt(abs(delta)):>9} {sign} {abs(pct):.0f}%")

    import shutil
    shutil.rmtree(TMPDIR, ignore_errors=True)
    print("\nDone.")

if __name__ == "__main__":
    main()
