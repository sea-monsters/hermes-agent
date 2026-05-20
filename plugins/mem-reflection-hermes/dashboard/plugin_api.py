"""Backend API for the mem-reflection-hermes dashboard plugin.

Exposes endpoints for memory graph visualization, skill inventory,
and reflection history.
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

# Ensure the plugin's __init__ is importable
plugin_dir = Path(__file__).resolve().parent.parent
if str(plugin_dir) not in sys.path:
    sys.path.insert(0, str(plugin_dir))

import __init__ as srh

router = APIRouter()


@router.get("/memories")
async def get_memories():
    """Return all active memories with metadata."""
    store = srh._get_mem_store()
    memories = store.list_active()
    return {
        "count": len(memories),
        "memories": [
            {
                "id": m.id(),
                "scope": m.scope,
                "body": m.body,
                "confidence": m.frontmatter.confidence,
                "pinned": m.frontmatter.pinned,
                "tags": m.frontmatter.tags,
                "supersedes": m.frontmatter.supersedes,
                "created": m.frontmatter.created,
                "source": m.frontmatter.source,
            }
            for m in memories
        ],
    }


@router.get("/skills")
async def get_skills():
    """Return all skills with metadata."""
    store = srh._get_skill_store()
    skills = store.list()
    return {
        "count": len(skills),
        "skills": [
            {
                "name": s.frontmatter.name,
                "description": s.frontmatter.description,
                "triggers": s.frontmatter.triggers,
                "scope": s.scope,
                "version": s.frontmatter.version,
                "license": s.frontmatter.license,
            }
            for s in skills
        ],
    }


@router.get("/reflections")
async def get_reflections(limit: int = 20):
    """Return recent reflection outcomes."""
    outcomes = srh._recent_reflect_outcomes(limit)
    return {"count": len(outcomes), "reflections": outcomes}


@router.get("/graph")
async def get_graph():
    """Return memory graph data for visualization (nodes + edges).

    Nodes: memories and skills.
    Edges: supersedes links, tag overlaps, skill triggers.
    """
    mem_store = srh._get_mem_store()
    skill_store = srh._get_skill_store()

    memories = mem_store.list_active()
    skills = skill_store.list()

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    # Memory nodes
    for m in memories:
        nodes.append({
            "id": m.id(),
            "type": "memory",
            "label": m.body[:60] + "..." if len(m.body) > 60 else m.body,
            "scope": m.scope,
            "confidence": m.frontmatter.confidence,
            "pinned": m.frontmatter.pinned,
            "tags": m.frontmatter.tags,
        })
        # Supersedes edges
        for old_id in m.frontmatter.supersedes:
            edges.append({
                "source": m.id(),
                "target": old_id,
                "type": "supersedes",
            })

    # Skill nodes
    for s in skills:
        nodes.append({
            "id": s.frontmatter.name,
            "type": "skill",
            "label": s.frontmatter.name,
            "description": s.frontmatter.description,
            "scope": s.scope,
            "triggers": s.frontmatter.triggers,
        })
        # Link skills to memories by tag overlap
        skill_tags = set(srh._skill_tokenise(s.frontmatter.name))
        skill_tags.update(srh._skill_tokenise(s.frontmatter.description))
        for t in s.frontmatter.triggers:
            skill_tags.update(srh._skill_tokenise(t))

        for m in memories:
            mem_tags = set(m.frontmatter.tags)
            mem_tags.update(srh._tokenise(m.body))
            overlap = skill_tags & mem_tags
            if overlap:
                edges.append({
                    "source": s.frontmatter.name,
                    "target": m.id(),
                    "type": "tag_overlap",
                    "overlap": list(overlap),
                })

    return {"nodes": nodes, "edges": edges}


@router.get("/stats")
async def get_stats():
    """Return aggregate statistics."""
    mem_store = srh._get_mem_store()
    skill_store = srh._get_skill_store()

    memories = mem_store.list_active()
    skills = skill_store.list()

    mem_by_scope: Dict[str, int] = {}
    mem_by_confidence: Dict[str, int] = {}
    tag_counts: Dict[str, int] = {}

    for m in memories:
        mem_by_scope[m.scope] = mem_by_scope.get(m.scope, 0) + 1
        c = m.frontmatter.confidence
        mem_by_confidence[c] = mem_by_confidence.get(c, 0) + 1
        for t in m.frontmatter.tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1

    return {
        "memory_count": len(memories),
        "skill_count": len(skills),
        "memories_by_scope": mem_by_scope,
        "memories_by_confidence": mem_by_confidence,
        "top_tags": sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:10],
    }
