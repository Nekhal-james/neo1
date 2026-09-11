# RAG data

The campus dataset: data, not code, which is why it lives outside the
`intelligence` Python package (same reasoning as `config/` living outside any
package). The directory is `rag.data_dir` in `config/intelligence.yaml`.

**No vector database. No embeddings. No chunk-and-similarity search.**
Retrieval is lookup and filtering over structured files, per CLAUDE.md's
"Vectorless RAG" invariant:

| File | Holds |
|---|---|
| `rooms.yaml` | one entry per place people ask for: `code`, `name`, `block`, `floor` (required), `type`, `wing`, `department`, `aliases`, `landmarks`, `directions`, `notes`, `node` |
| `graph.yaml` | optional walkable node/edge graph, for directions from the `reception` node |
| `coverage.yaml` | per-block survey status (`complete` / `partial` / `not_surveyed`), so Neo can say "I haven't learned C block yet" instead of inventing a room |

All three are optional and none is committed yet: campus data is entered and
edited on the admin panel's **Data** tab, which shows the format, a working
example of each file, and how to write entries Neo can find. The same examples
live in `intelligence/campus.py` (`SAMPLES`) and are validated by the tests.

The code:

- `intelligence/campus.py` — parsing, validation (unknown fields are errors, so a
  typo cannot silently drop data), atomic saves with backups under
  `rag.backup_dir`.
- `intelligence/retrieval.py` — normalises a question and matches it as whole
  phrases against codes, names and aliases; renders the "Campus directory"
  section of the system prompt from only the matched entries, and a template
  answer for when no model host is reachable.

`scripts/pi/sync-to-pi.sh` never copies the `*.yaml` files here: the Pi's panel
is where they are edited, and a sync from the laptop must not overwrite that.
