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
import tempfile
import time
from pathlib import Path

from . import tls
from .config import Config, discover_model
from .status_store import HOST_HEARTBEAT_S, write_host_status

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


def _ollama_env(port: int) -> dict:
    """Env for `ollama` CLI subprocesses so they talk to the server this
    module started (internal port when TLS is on), not the CLI default 11434."""
    import os

    env = dict(os.environ)
    env["OLLAMA_HOST"] = f"127.0.0.1:{port}"
    return env


def _model_is_registered(name: str, *, env: dict) -> bool:
    """Whether `ollama list` already has this model registered."""
    check = subprocess.run(
        ["ollama", "list"], capture_output=True, text=True, env=env
    )
    stdout = getattr(check, "stdout", "") or ""
    return name in stdout


def ensure_model(model_path_or_name: str, *, port: int) -> str:
    """Make sure the model is available to Ollama; returns the model name to run.

    A .gguf path is registered via a generated Modelfile (`ollama create`); a
    bare name is pulled from the registry if not already present. Either way,
    an already-registered model is reused -- `ollama create` is only called
    when the name is absent, so repeated `neo --model up` runs do not rebuild
    the same GGUF every time.
    """
    env = _ollama_env(port)
    p = Path(model_path_or_name)
    if p.suffix == ".gguf":
        if not p.exists():
            raise OllamaError(f"model file not found: {p}")
        name = _model_name_for(model_path_or_name)
        if not _model_is_registered(name, env=env):
            with tempfile.TemporaryDirectory() as tmpdir:
                modelfile = Path(tmpdir) / "Modelfile"
                modelfile.write_text(f"FROM {p}\n", encoding="utf-8")
                subprocess.run(
                    ["ollama", "create", name, "-f", str(modelfile)],
                    check=True,
                    env=env,
                )
        else:
            log.info("model %r already registered; reusing it", name)
        return name

    name = model_path_or_name
    if not _model_is_registered(name, env=env):
        subprocess.run(["ollama", "pull", name], check=True, env=env)
    return name


def warm(model_name: str, *, port: int, keep_alive_minutes: int,
         timeout_s: float = 180.0) -> float | None:
    """Load the model into memory now, so the first real question does not.

    Ollama loads a model lazily on first use, and a 3B load costs ~45 s. Doing
    that lazily means whoever asks Neo the first question after a quiet spell
    waits three quarters of a minute or, more likely, times out and gets the
    degraded reply. A reception desk is quiet most of the time, so that is not
    an edge case -- it is the common path.

    An empty prompt with `keep_alive` set is Ollama's documented way to load a
    model without generating anything. Returns the load time in seconds, or
    None if warming failed -- which is never fatal: a cold model still answers,
    just slowly.
    """
    import requests

    try:
        started = time.monotonic()
        resp = requests.post(
            f"http://127.0.0.1:{port}/api/generate",
            json={"model": model_name, "keep_alive": f"{int(keep_alive_minutes)}m"},
            timeout=(2.0, timeout_s),
        )
        resp.raise_for_status()
        return time.monotonic() - started
    except Exception as exc:  # noqa: BLE001 -- warming is an optimisation
        log.warning("could not pre-load %s: %s", model_name, exc)
        return None


def up(cfg: Config, model_override: str | None) -> int:
    """`neo [--model PATH] up` -- resolve the model, ensure Ollama is serving it
    (behind the mTLS proxy when tls.enabled), print the endpoint, and block
    until interrupted."""
    if not is_ollama_installed():
        print("[model-conn] ollama not found on PATH -- install it first: https://ollama.com")
        return 1

    model = model_override or cfg.host.default_model_path
    if not model:
        model = discover_model()
    if not model:
        print(
            "[model-conn] no model path given and none configured "
            "(pass --model, set host.default_model_path in config, "
            "or place a .gguf file in models/)"
        )
        return 1

    if cfg.tls.enabled:
        try:
            tls.require_server_certs(cfg)
        except tls.TlsError as exc:
            print(f"[model-conn] {exc}")
            return 1
        # Ollama itself only ever binds loopback here -- the proxy takes the
        # public interface and is the only thing that verifies a client cert.
        ollama_host, ollama_port = "127.0.0.1", cfg.host.internal_ollama_port
    else:
        tls.warn_insecure("ollama serve")
        ollama_host, ollama_port = cfg.host.bind_host, cfg.host.ollama_port

    if not is_serving(ollama_port):
        log.info("starting ollama serve on %s:%d", ollama_host, ollama_port)
        proc = start_serve(ollama_port, ollama_host)
        for _ in range(50):
            if is_serving(ollama_port):
                break
            time.sleep(0.2)
        else:
            print("[model-conn] ollama serve did not come up in time")
            return 1
    else:
        proc = None

    try:
        model_name = ensure_model(model, port=ollama_port)
    except OllamaError as exc:
        print(f"[model-conn] {exc}")
        return 1

    # Warm before announcing the endpoint: once the endpoint is printed the
    # thing is advertised as ready, and a model that takes 45 s to answer its
    # first question is not ready.
    held = warm(
        model_name, port=ollama_port, keep_alive_minutes=cfg.host.idle_unload_minutes
    )
    if held is not None:
        print(
            f"[model-conn] pre-loaded '{model_name}' in {held:.1f}s; "
            f"holding it for {cfg.host.idle_unload_minutes} min of idle"
        )

    if cfg.tls.enabled:
        from .tls_proxy import start_proxy_thread

        start_proxy_thread(cfg)
        endpoint = f"https://{cfg.host.bind_host}:{cfg.host.ollama_port}"
        print(f"[model-conn] serving '{model_name}' behind mTLS at {endpoint}")
    else:
        endpoint = f"http://{ollama_host}:{ollama_port}"
        print(f"[model-conn] serving '{model_name}' at {endpoint}")
    print("[model-conn] press Ctrl-C to stop watching (ollama keeps running)")

    stop_heartbeat = _start_host_heartbeat(
        cfg,
        model_name=model_name,
        model_path=model,
        endpoint=endpoint,
    )
    try:
        if proc is None:
            # Already running before we got here -- nothing to tail, just idle.
            while True:
                time.sleep(1.0)
        else:
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="")
    except KeyboardInterrupt:
        pass
    finally:
        stop_heartbeat()
    return 0


def _start_host_heartbeat(
    cfg: Config, *, model_name: str, model_path: str, endpoint: str
):
    """Publish what we are serving, and keep saying so.

    A heartbeat rather than a single write, because the interesting question a
    reader asks is "is a model up *now*". A one-shot file would keep claiming
    the model was up long after this process was killed, and killed is the
    normal way a foreground `up` ends.

    Returns a callable that marks the host stopped and joins the thread.
    """
    import threading

    path = cfg.host_status_path
    stopping = threading.Event()

    def publish(serving: bool) -> None:
        write_host_status(
            serving=serving,
            model_name=model_name,
            model_path=model_path,
            endpoint=endpoint,
            tls_enabled=cfg.tls.enabled,
            path=path,
        )

    publish(True)
    log.info("publishing host status to %s", path)

    def beat() -> None:
        while not stopping.wait(HOST_HEARTBEAT_S):
            publish(True)

    thread = threading.Thread(target=beat, name="host-status", daemon=True)
    thread.start()

    def stop() -> None:
        stopping.set()
        thread.join(timeout=1.0)
        # Makes a clean Ctrl-C visible to the panel immediately, instead of
        # after the staleness timeout.
        publish(False)

    return stop
