# mem-reflection-hermes

Self-evolving memory & reflection system for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Ported from [small-rust-hermes](https://github.com/coder-brzhang/small-rust-hermes) with significant performance enhancements.

## Features

- **Structured Memories**: Markdown + YAML frontmatter (id, created, source, confidence, pinned, tags, supersedes)
- **Dual Scope**: User-level (`~/.hermes/memories/`) and project-level (`./.hermes/memories/`)
- **Memory Palace**: Zone-based organization (core, work, episode, general, project:*) with tool-driven navigation
- **TF-IDF Search**: Pure Python implementation, zero external dependencies, ~1ms for 50 memories
- **Semantic Search**: ONNX Runtime + all-MiniLM-L6-v2, 16x faster than PyTorch (optional)
- **Conflict Detection**: Automatic similarity checking on write with supersedes chains
- **Effectiveness Tracking**: Per-memory effectiveness scoring with time decay
- **Micro-Reflection**: Per-turn background reflection with CJK-aware token estimation
- **Full Reflection**: Session-end structured summary with human approval for skills
- **Skill Auto-Matching**: Token overlap + optional embedding hybrid for context injection
- **Context Layering**: Pinned → Active Index → Triggered Skills → Always-Active Skills
- **Profile Compilation**: LLM-driven compilation of all memories into structured profile documents

## Architecture

```
┌─────────────────────────────────────────┐
│           Hermes Agent Session          │
├─────────────────────────────────────────┤
│  pre_llm_call hook                      │
│    ├── Inject palace index (zone map)   │
│    ├── Inject compiled profile          │
│    ├── Inject triggered/always skills   │
│    └── Trigger micro-reflection         │
├─────────────────────────────────────────┤
│  on_session_end hook                    │
│    └── Run full reflection              │
├─────────────────────────────────────────┤
│  Tools (9)                              │
│    ├── srh_memory_search                │
│    ├── srh_memory_write                 │
│    ├── srh_memory_delete                │
│    ├── srh_skill_search                 │
│    ├── srh_reflect_now                  │
│    ├── srh_palace_zones                 │
│    ├── srh_palace_read_zone             │
│    ├── srh_palace_recall                │
│    └── srh_compile_profile              │
├─────────────────────────────────────────┤
│  Slash Commands (7)                     │
│    ├── /memories                        │
│    ├── /skills                          │
│    ├── /pending-skills                  │
│    ├── /approve-skill <id>              │
│    ├── /reject-skill <id>               │
│    ├── /reflect                         │
│    └── /compile-profile                 │
└─────────────────────────────────────────┘
```

## Performance

### v0.5.0 Optimization Results (50 memories, 10 skills, TF-IDF only)

| Metric | v0.4.0 | v0.5.0 | Improvement | Mechanism |
|--------|--------|--------|-------------|-----------|
| Context Block (warm) | 1.74ms | 1.31ms | ↓ 25% | Write-on-change + event-driven index + SkillStore cache |
| Memory Write (agent perceived) | 11.69ms | 0.57ms | ↓ 95% | Async I/O (P2-2) |
| Memory Delete | 10.68ms | 0.14ms | ↓ 99% | O(1) id→path index (P0-2) |
| Token Estimation | 2.96ms | 0.6µs | ↓ 5000x | Byte-based fast estimation (P1-1) |
| Stat Flush | 206µs | 4.4µs | ↓ 98% | Async queue (P1-2) |
| Skill Search | 7µs | 3µs | ↓ 57% | SkillStore lazy cache |
| **Total Hot Path** | **76.9ms** | **34.0ms** | **↓ 56%** | |

> Full benchmark: `python bench_latency.py` (requires 50 test memories)

## Changelog

### v0.5.0 — Performance Optimization Release
- **P0-1**: Palace index write-on-change — skip disk write when content hasn't changed
- **P0-2**: O(1) delete via id→path reverse index — eliminates directory scan
- **P1-1**: Fast byte-based token estimation — 5000x faster than char-by-char CJK check
- **P1-2**: Async stat flush — background thread for JSONL recording
- **P2-1**: Event-driven palace index rebuild — only rebuild on memory mutation
- **P2-2**: Async memory write — background thread for file I/O
- **SkillStore lazy cache**: One-time file read per session
- **build_palace_index cache**: Cached result string on MemoryStore
- Fix: `build_palace_index` sort key type error (int vs tuple comparison)

### v0.4.0 — Memory Palace + Profile
- Memory Palace zone-based organization with 3 navigation tools
- Effectiveness tracking with time decay
- LLM-compiled profile generation (profile.md, palace-index.md, zone-cache)
- Always-active skills, supersedes chains
- CJK-aware token estimation for context limits
- Configurable caps (memory/skill/trigger limits)

## Installation

### Prerequisites

- Python 3.10+
- Hermes Agent v3.2.2+
- ONNX Runtime (optional, for semantic search)

### Quick Start

```bash
# 1. Clone into Hermes plugins directory
cd ~/.hermes/plugins
git clone https://github.com/sea-monsters/mem-reflection-hermes.git

# 2. Enable in Hermes config
cat >> ~/.hermes/config.yaml << 'EOF'
plugins:
  enabled:
    - mem-reflection-hermes
  mem_reflection_hermes:
    embeddings: false         # TF-IDF only (fast, zero deps)
    micro_reflection: true    # Auto-reflect per turn
    palace_mode: true         # Memory Palace navigation
EOF

# 3. Restart Hermes Agent
hermes restart
```

### Optional: ONNX Model Setup (Recommended)

For best performance, use the ONNX model instead of sentence-transformers fallback:

```bash
# Install dependencies
pip install onnxruntime tokenizers

# Download and convert model
python -c "
from optimum.onnxruntime import ORTModelForFeatureExtraction
from transformers import AutoTokenizer
import os

model_id = 'sentence-transformers/all-MiniLM-L6-v2'
output_dir = os.path.expanduser('~/.hermes/models/all-MiniLM-L6-v2-onnx')

model = ORTModelForFeatureExtraction.from_pretrained(model_id, export=True)
tokenizer = AutoTokenizer.from_pretrained(model_id)

model.save_pretrained(output_dir)
tokenizer.save_pretrained(output_dir)
print(f'Model saved to {output_dir}')
"
```

Or set a custom model directory via environment variable:

```bash
export SRH_MODEL_DIR=/path/to/your/onnx-model
```

### Alternative: sentence-transformers Fallback

If you skip ONNX setup, the plugin automatically falls back to sentence-transformers:

```bash
pip install sentence-transformers
```

## Configuration

All configuration lives in `~/.hermes/config.yaml` under the `plugins.mem_reflection_hermes` section:

```yaml
plugins:
  enabled:
    - mem-reflection-hermes
  
  mem_reflection_hermes:
    # Core features
    embeddings: false              # Enable semantic search (default: true)
    micro_reflection: true         # Auto-reflect per turn (default: false)
    palace_mode: true              # Memory Palace navigation (default: true)
    profile_mode: false            # LLM-compiled profile injection (default: false)
    palace_instructions: true      # Inject palace usage instructions (default: true)
    
    # Capacity limits
    active_memory_index_cap: 50    # Max memories in active index (default: 50)
    skill_index_cap: 50            # Max skills in index (default: 50)
    relevant_memory_cap: 3         # Max per-turn relevant memories (default: 3)
    triggered_skill_cap: 3         # Max per-turn triggered skills (default: 3)
    max_context_token_preference: 6000  # Token budget for context block
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HERMES_HOME` | Hermes configuration directory | `~/.hermes` |
| `SRH_MODEL_DIR` | Custom ONNX model directory | `~/.hermes/models/all-MiniLM-L6-v2-onnx` |

## Usage

### Automatic Behavior

Once enabled, the plugin works automatically:

1. **Session Start**: Builds palace index, loads compiled profile if available
2. **Per Turn**: Injects layered context (palace/profile/pinned) into user message
3. **Session End**: Runs full reflection, generates skill candidates

### Manual Tools

```
# Search memories
srh_memory_search(query="Python error handling", k=5)

# Write a memory
srh_memory_write(
    body="Always use anyhow for app-level error handling",
    tags=["rust", "error-handling"],
    confidence="high",
    pinned=true
)

# Delete a memory
srh_memory_delete(memory_id="mem_abc123")

# Search skills
srh_skill_search(query="rust async", k=3)

# Trigger manual reflection
srh_reflect_now(mode="full")
```

### Slash Commands

```
/memories              # List all active memories
/skills                # List all active skills
/pending-skills        # Show skills awaiting approval
/approve-skill <id>    # Approve a pending skill
/reject-skill <id>     # Reject a pending skill
/compile-profile       # Compile memories into profile via LLM
```

## Memory Format

Memories are stored as plain Markdown files with YAML frontmatter:

```markdown
---
id: mem_abc123
created: 2024-01-15T10:30:00Z
source: micro_reflection
confidence: high
pinned: false
tags:
  - python
  - rust
zone: general
supersedes: []
---

Always use anyhow for app-level error handling in Rust.
```

This format is:
- **Human-readable**: View and edit with any text editor
- **Git-friendly**: Track changes over time
- **Portable**: Easy to export/import

## File Structure

```
~/.hermes/
├── memory/
│   ├── memories/                     # User-level memories
│   │   └── 2024-01-15-mem_abc12.md
│   ├── skills/                       # User-level skills
│   │   └── rust-error-handling/
│   │       └── SKILL.md
│   ├── zone-cache/                   # Per-zone summary caches
│   ├── palace-index.md              # Palace zone index
│   └── memory-stats.jsonl           # Effectiveness tracking
├── plugins/
│   └── mem-reflection-hermes/
│       ├── __init__.py              # Main plugin (~3,400 lines)
│       ├── plugin.yaml              # Plugin manifest
│       ├── bench_latency.py         # Performance benchmark
│       └── PERF_REPORT.md           # Optimization report
└── models/                          # ONNX model (optional)
    └── all-MiniLM-L6-v2-onnx/
        ├── model.onnx
        └── tokenizer.json
```

## Development

### Running Tests

```bash
cd ~/.hermes/plugins/mem-reflection-hermes
python -m pytest tests/ -v
```

### Performance Benchmarking

```bash
# Full latency benchmark
python bench_latency.py

# Profile a specific function
python -c "
import cProfile, pstats
pr = cProfile.Profile()
pr.enable()
# ... run target code ...
pr.disable()
pstats.Stats(pr).sort_stats('cumtime').print_stats(20)
"
```

## License

MIT - Ported from [coder-brzhang/small-rust-hermes](https://github.com/coder-brzhang/small-rust-hermes)

## Acknowledgments

- Original Rust implementation by [coder-brzhang](https://github.com/coder-brzhang)
- ONNX optimization inspired by [optimum](https://github.com/huggingface/optimum)
- Embedding model: [sentence-transformers/all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)
