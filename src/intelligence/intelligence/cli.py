"""What `neo --prompt "<text>"` calls -- see model_conn/__main__.py.

Kept as its own module (rather than inline in model_conn) so `intelligence`
owns its own CLI surface, the same separation model_conn keeps between
__main__.py's dispatch and ollama.py's/link.py's actual logic.
"""

from __future__ import annotations

from model_conn.config import Config as ModelConnConfig

from .chat import ask
from .config import Config


def run_prompt(text: str, *, mc_cfg: ModelConnConfig, cfg: Config | None = None) -> int:
    cfg = cfg or Config.load()
    result = ask(text, cfg=cfg, mc_cfg=mc_cfg)

    print(result.reply)
    if result.source == "degraded":
        return 1
    return 0
