# RAG data (placeholder)

Empty for now. This is where the campus classroom/location dataset goes once
Phase 7 of [the implementation plan](../../../docs/IMPLEMENTATION_PLAN.md)
is built — data, not code, which is why it lives outside the `intelligence`
Python package (same reasoning as `config/` living outside any package).

**No vector database. No embeddings. No chunk-and-similarity search.**
Retrieval is meant to be pure lookup/filter/prompt-stuffing over structured
files, per CLAUDE.md's "Vectorless RAG" invariant:

| File | Holds |
|---|---|
| `data/rooms.yaml` | code, name, type, block, floor, wing, department, node, aliases, landmarks — one entry per known room |
| `data/graph.yaml` | a walkable node/edge graph between rooms/landmarks, with human-readable `instruction` text per edge |
| `data/coverage.yaml` | per-block/floor survey status (`complete` / `partial` / `not_surveyed`) — this is what lets the assistant say "I haven't learned that block yet" instead of inventing a room |

None of these files exist yet — `data/` is empty (just a `.gitkeep`). Real
campus data arrives incrementally and is edited through the admin panel, not
committed by hand; the retrieval logic that reads these files is Phase 7
scope and isn't implemented in this package yet either.
