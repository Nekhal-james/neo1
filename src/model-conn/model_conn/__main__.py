"""`neo` -- CLI for both ends of the off-board LLM link.

Host role (the laptop running Qwen 2.5 3B):
    neo --model path/to/model.gguf up

Receiver role (the Pi):
    neo --connection:status
    neo --connection:ping
"""

from __future__ import annotations

import argparse
import sys

from . import link, ollama, tls
from .config import Config
from .status_store import write_status


def build_parser() -> argparse.ArgumentParser:
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
    p.add_argument("--config", metavar="PATH", help="override config/model_conn.yaml")
    p.add_argument("command", nargs="?", choices=["up"], help="host: start serving the model")
    return p


def _cmd_status(cfg: Config) -> int:
    tls.warn_insecure("connection:status")
    tracker = link.LinkTracker(cfg.receiver.endpoints, cfg.receiver.probe_timeout_s)
    status = tracker.check_once()
    write_status(status, None, path=cfg.status_path)

    if status.up:
        print(f"UP    path={status.active_path}  rtt={status.rtt_ms:.1f}ms  failures=0")
        return 0
    print(f"DOWN  path=none  failures={status.consecutive_failures}")
    return 1


def _cmd_ping(cfg: Config) -> int:
    tls.warn_insecure("connection:ping")
    stats = link.run_ping(
        cfg.receiver.endpoints,
        count=cfg.receiver.ping_count,
        timeout_s=cfg.receiver.probe_timeout_s,
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config.load(args.config)

    if args.command == "up":
        return ollama.up(cfg, args.model)
    if args.connection_status:
        return _cmd_status(cfg)
    if args.connection_ping:
        return _cmd_ping(cfg)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
