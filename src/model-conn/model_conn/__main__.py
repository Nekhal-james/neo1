"""`neo` -- CLI for both ends of the off-board LLM link, and the admin panel.

Host role (the laptop running Qwen 2.5 3B):
    neo --model path/to/model.gguf up

Receiver role (the Pi):
    neo --connection:status
    neo --connection:ping

Admin panel (either machine):
    neo --webapp up
    neo --webapp setup
    neo --webapp devcert

Test the assistant directly, e.g. on the Pi:
    neo --prompt "where is CS-204"

Set up mTLS (run once on the host, then copy the client cert/key + CA cert
to the Pi -- see the printed paths):
    neo --tls init

`--webapp ...` forwards every flag it doesn't itself recognize straight to
neo_webapp's own CLI -- including neo_webapp's own `--config`, which is a
*different* file than the one below. model_conn's `--config` only applies to
the non-webapp commands (`up`, `--connection:status`, `--connection:ping`,
`--prompt`).
"""

from __future__ import annotations

import argparse
import sys

from . import link, ollama, tls
from .config import Config
from .status_store import write_status

WEBAPP_SUBCOMMANDS = ("up", "setup", "devcert")
TLS_SUBCOMMANDS = ("init",)


def _webapp_probe() -> argparse.ArgumentParser:
    """Only knows about `--webapp` -- deliberately does NOT define a
    positional `command`, `--config`, `--model`, etc. A positional with
    `choices=` here would make argparse hard-fail (not just fall into
    `extra`) on ANY other flag's value that happens to appear first, e.g.
    `neo --prompt "hello"` -- `--prompt` is unrecognized by this probe, so
    argparse would try to consume "hello" as the (choice-restricted)
    subcommand and crash. The subcommand is pulled out of `remainder`
    manually instead, in `main()`."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--webapp", action="store_true")
    return p


def _tls_probe() -> argparse.ArgumentParser:
    """Same reasoning as `_webapp_probe`, except `--tls` never forwards to
    another program's CLI, so it's safe to also own `--config` here directly
    rather than needing a second pass."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--tls", action="store_true")
    p.add_argument("--config", metavar="PATH")
    return p


def build_parser() -> argparse.ArgumentParser:
    """The non-webapp surface: host/receiver/prompt commands. `command` here
    only ever means `up` (start serving a model) -- `setup`/`devcert` are
    webapp-only subcommands handled by `_webapp_parser` before this is used."""
    p = argparse.ArgumentParser(
        prog="neo", description="Model connection CLI (host + receiver)."
    )
    p.add_argument("--model", metavar="PATH", help="path to a .gguf model (host role, with 'up')")
    p.add_argument(
        "--connection:status",
        dest="connection_status",
        action="store_true",
        help="receiver: check the link to the off-board host",
    )
    p.add_argument(
        "--connection:ping",
        dest="connection_ping",
        action="store_true",
        help="receiver: run several probes and print latency/loss stats",
    )
    p.add_argument(
        "--webapp",
        action="store_true",
        help="run the admin panel instead of the model link -- see 'neo --webapp'",
    )
    p.add_argument(
        "--prompt",
        metavar="TEXT",
        help="ask the assistant TEXT directly, print the reply, exit (test path)",
    )
    p.add_argument("--config", metavar="PATH", help="override config/model_conn.yaml")
    p.add_argument("command", nargs="?", choices=["up"], help="host: start serving the model")
    return p


def _cmd_webapp(subcommand: str, extra_args: list[str]) -> int:
    """Forward to neo_webapp's own CLI/scripts, which own --host/--port/
    --backend/--config/etc. `extra_args` is whatever `_webapp_parser` didn't
    recognize."""
    targets = {
        "up": ("neo_webapp.__main__", "main"),
        "setup": ("neo_webapp.scripts.setup_admin", "main"),
        "devcert": ("neo_webapp.scripts.make_dev_cert", "main"),
    }
    module_name, attr = targets[subcommand]
    try:
        import importlib

        target = getattr(importlib.import_module(module_name), attr)
    except ImportError as exc:
        # Don't swallow *why*: this also fires if neo_webapp imports fine but
        # one of ITS dependencies (fastapi, uvicorn, ...) doesn't -- that's a
        # very different fix than "package not installed".
        print(f"[model-conn] could not import neo_webapp: {exc}")
        print("[model-conn] pip install -e \".[dev]\" from the repo root")
        return 1
    return target(extra_args)


def _cmd_status(cfg: Config) -> int:
    if not cfg.tls.enabled:
        tls.warn_insecure("connection:status")
    try:
        transport = link.transport_for(cfg)
    except tls.TlsError as exc:
        print(f"[model-conn] {exc}")
        return 1
    tracker = link.LinkTracker(cfg.receiver.endpoints, cfg.receiver.probe_timeout_s, transport=transport)
    status = tracker.check_once()
    write_status(status, None, path=cfg.status_path)

    if status.up:
        print(f"UP    path={status.active_path}  rtt={status.rtt_ms:.1f}ms  failures=0")
        return 0
    print(f"DOWN  path=none  failures={status.consecutive_failures}")
    return 1


def _cmd_ping(cfg: Config) -> int:
    if not cfg.tls.enabled:
        tls.warn_insecure("connection:ping")
    try:
        transport = link.transport_for(cfg)
    except tls.TlsError as exc:
        print(f"[model-conn] {exc}")
        return 1
    stats = link.run_ping(
        cfg.receiver.endpoints,
        count=cfg.receiver.ping_count,
        timeout_s=cfg.receiver.probe_timeout_s,
        transport=transport,
    )
    link_status = link.LinkStatus(
        up=stats.received > 0,
        active_path=stats.active_path,
        rtt_ms=stats.rtt_avg_ms,
        consecutive_failures=0 if stats.received > 0 else 1,
    )
    write_status(link_status, stats, path=cfg.status_path)

    print(f"{stats.sent} sent, {stats.received} received, {stats.loss_pct:.0f}% loss")
    if stats.received > 0:
        print(
            f"rtt min/avg/max = {stats.rtt_min_ms:.1f}/{stats.rtt_avg_ms:.1f}/"
            f"{stats.rtt_max_ms:.1f} ms (path={stats.active_path})"
        )
        return 0
    return 1


def _cmd_tls_init(cfg: Config) -> int:
    """`neo --tls init` -- generate the CA (once) plus a fresh server cert
    (for this machine) and client cert (for the receiver). Safe to re-run:
    the CA is never overwritten once created; server/client certs are
    reissued every time, e.g. after adding a new receiver.endpoints host."""
    from . import certs

    certs.generate_ca(cfg.ca_cert_path, cfg.ca_key_path)

    local_hosts, local_ips = certs.local_hostnames_and_ips()
    endpoint_hosts = [e.host for e in cfg.receiver.endpoints if e.host]
    # Endpoint hosts may be IPs or DNS names -- split so each goes in the
    # SAN type that actually validates.
    endpoint_ips, endpoint_dns = [], []
    for h in endpoint_hosts:
        try:
            import ipaddress

            ipaddress.ip_address(h)
            endpoint_ips.append(h)
        except ValueError:
            endpoint_dns.append(h)

    certs.generate_server_cert(
        ca_cert_path=cfg.ca_cert_path,
        ca_key_path=cfg.ca_key_path,
        cert_path=cfg.server_cert_path,
        key_path=cfg.server_key_path,
        dns_names=[*local_hosts, *endpoint_dns],
        ip_addresses=[*local_ips, *endpoint_ips],
    )
    certs.generate_client_cert(
        ca_cert_path=cfg.ca_cert_path,
        ca_key_path=cfg.ca_key_path,
        cert_path=cfg.client_cert_path,
        key_path=cfg.client_key_path,
    )

    print(f"[model-conn] CA:            {cfg.ca_cert_path}")
    print(f"[model-conn] server cert:   {cfg.server_cert_path}")
    print(f"[model-conn] client cert:   {cfg.client_cert_path}")
    print(
        "\nSet tls.enabled: true in config/model_conn.local.yaml on BOTH machines.\n"
        f"Copy {cfg.ca_cert_path.name}, {cfg.client_cert_path.name}, and "
        f"{cfg.client_key_path.name} to the Pi's certs/model_conn/ directory -- "
        "the server cert/key stay on this machine only."
    )
    return 0


def _cmd_tls(subcommand: str, cfg: Config) -> int:
    if subcommand == "init":
        return _cmd_tls_init(cfg)
    print(f"[model-conn] usage: neo --tls {{{','.join(TLS_SUBCOMMANDS)}}}")
    return 1


def _cmd_prompt(text: str, cfg: Config) -> int:
    """Ask the assistant directly -- the test path for `neo --prompt`."""
    try:
        from intelligence.cli import run_prompt
    except ImportError as exc:
        print(f"[model-conn] could not import intelligence: {exc}")
        print("[model-conn] pip install -e \".[dev]\" from the repo root")
        return 1
    return run_prompt(text, mc_cfg=cfg)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])

    # --webapp is handled by its own minimal parser, before --config/--model/
    # etc exist at all, so those flags are free to forward to neo_webapp.
    webapp_args, remainder = _webapp_probe().parse_known_args(argv)
    if webapp_args.webapp:
        usage = f"[model-conn] usage: neo --webapp {{{','.join(WEBAPP_SUBCOMMANDS)}}} [flags...]"
        if not remainder or remainder[0].startswith("-"):
            print(usage)
            return 1
        subcommand, forward = remainder[0], remainder[1:]
        if subcommand not in WEBAPP_SUBCOMMANDS:
            print(usage)
            return 1
        return _cmd_webapp(subcommand, forward)

    tls_args, tls_remainder = _tls_probe().parse_known_args(argv)
    if tls_args.tls:
        usage = f"[model-conn] usage: neo --tls {{{','.join(TLS_SUBCOMMANDS)}}}"
        if not tls_remainder or tls_remainder[0].startswith("-"):
            print(usage)
            return 1
        subcommand = tls_remainder[0]
        if subcommand not in TLS_SUBCOMMANDS:
            print(usage)
            return 1
        return _cmd_tls(subcommand, Config.load(tls_args.config))

    parser = build_parser()
    args = parser.parse_args(argv)

    modes = {
        "prompt": args.prompt is not None,
        "up": args.command == "up",
        "connection:status": args.connection_status,
        "connection:ping": args.connection_ping,
    }
    selected = [name for name, on in modes.items() if on]
    if len(selected) > 1:
        parser.error(f"choose only one of: {', '.join(selected)} (got {len(selected)})")

    cfg = Config.load(args.config)

    if modes["prompt"]:
        if not args.prompt.strip():
            parser.error("--prompt must not be empty")
        return _cmd_prompt(args.prompt, cfg)
    if modes["up"]:
        return ollama.up(cfg, args.model)
    if modes["connection:status"]:
        return _cmd_status(cfg)
    if modes["connection:ping"]:
        return _cmd_ping(cfg)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
