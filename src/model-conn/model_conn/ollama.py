"""Host-role: subprocess-manage `ollama serve` so a .gguf model is reachable
over an OpenAI-compatible API, the way `neo --model ... up` is meant to feel
like `ollama serve`.

Ollama-only, deliberately: this repo has one model (Qwen 2.5 3B) and one host,
so a pluggable backend abstraction would be unused generality. Kept isolated to
this module so a future swap (e.g. llama.cpp) touches only this file.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from . import tls
from .config import Config

log = logging.getLogger("model_conn.ollama")


class OllamaError(RuntimeError):
    pass


def is_ollama_installed() -> bool:
    return shutil.which("ollama") is not None


def is_serving(port: int, *, timeout_s: float = 1.0) -> bool:
    import requests

    try:
        resp = requests.get(f"http://127.0.0.1:{port}/api/tags", timeout=timeout_s)
        return resp.ok
    except requests.RequestException:
        return False


def start_serve(port: int, bind_host: str) -> subprocess.Popen:
    """Launch `ollama serve` in the background. Idempotent: check is_serving first."""
    import os

    env = dict(os.environ)
    env["OLLAMA_HOST"] = f"{bind_host}:{port}"
    proc = subprocess.Popen(
        ["ollama", "serve"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc


def _model_name_for(model_path_or_name: str) -> str:
    """A .gguf path becomes a local Ollama model name; anything else is used as-is
    (a registry name like "qwen2.5:3b")."""
    p = Path(model_path_or_name)
    if p.suffix == ".gguf":
        return p.stem.lower().replace(" ", "-")
    return model_path_or_name


def ensure_model(model_path_or_name: str, *, port: int) -> str:
    """Make sure the model is available to Ollama; returns the model name to run.

    A .gguf path is registered via a generated Modelfile (`ollama create`); a
    bare name is pulled from the registry if not already present.
    """
    p = Path(model_path_or_name)
    if p.suffix == ".gguf":
        if not p.exists():
            raise OllamaError(f"model file not found: {p}")
        name = _model_name_for(model_path_or_name)
        modelfile = p.parent / f".{name}.Modelfile"
        modelfile.write_text(f"FROM {p}\n", encoding="utf-8")
        subprocess.run(
            ["ollama", "create", name, "-f", str(modelfile)], check=True
        )
        return name

    name = model_path_or_name
    check = subprocess.run(["ollama", "list"], capture_output=True, text=True)
    if name not in check.stdout:
        subprocess.run(["ollama", "pull", name], check=True)
    return name


def up(cfg: Config, model_override: str | None) -> int:
    """`neo [--model PATH] up` -- resolve the model, ensure Ollama is serving it,
    print the endpoint, and block until interrupted."""
    if not is_ollama_installed():
        print("[model-conn] ollama not found on PATH -- install it first: https://ollama.com")
        return 1

    model = model_override or cfg.host.default_model_path
    if not model:
        print(
            "[model-conn] no model path given and none configured "
            "(pass --model or set host.default_model_path in config/model_conn.yaml)"
        )
        return 1

    tls.warn_insecure("ollama serve")

    port = cfg.host.ollama_port
    if not is_serving(port):
        log.info("starting ollama serve on %s:%d", cfg.host.bind_host, port)
        proc = start_serve(port, cfg.host.bind_host)
        for _ in range(50):
            if is_serving(port):
                break
            time.sleep(0.2)
        else:
            print("[model-conn] ollama serve did not come up in time")
            return 1
    else:
        proc = None

    try:
        model_name = ensure_model(model, port=port)
    except OllamaError as exc:
        print(f"[model-conn] {exc}")
        return 1

    print(f"[model-conn] serving '{model_name}' at http://{cfg.host.bind_host}:{port}")
    print("[model-conn] press Ctrl-C to stop watching (ollama keeps running)")

    if proc is None:
        # Already running before we got here -- nothing to tail, just idle.
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            return 0

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="")
    except KeyboardInterrupt:
        pass
    return 0
