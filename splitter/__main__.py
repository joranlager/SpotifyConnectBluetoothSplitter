"""Command line entry point: `python -m splitter <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import load_config


def _cmd_list(_: argparse.Namespace) -> int:
    from .outputs import list_outputs

    outputs = list_outputs()
    if not outputs:
        print("No audio output devices found. Pair your Bluetooth speakers in Windows first.")
        return 1
    print(f"{'idx':>4}  {'host API':<22} name")
    for o in outputs:
        print(f"{o.index:>4}  {o.hostapi:<22} {o.name}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from .app import run_engine_with_web

    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        print(f"config file not found: {args.config} (see config.example.json)", file=sys.stderr)
        return 2
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2
    if args.name:
        cfg.device_name = args.name
    if args.port:
        cfg.web_port = args.port
    if args.host:
        cfg.web_host = args.host
    return run_engine_with_web(cfg, args.config, web=not args.no_web)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="splitter", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list audio output devices").set_defaults(func=_cmd_list)

    run_p = sub.add_parser("run", help="start the Spotify Connect device")
    run_p.add_argument("-c", "--config", default="config.json", help="path to JSON config")
    run_p.add_argument("--name", help="override device_name from the config")
    run_p.add_argument("--host", help="override web_host (e.g. 0.0.0.0 for LAN access)")
    run_p.add_argument("--port", type=int, help="override web_port")
    run_p.add_argument("--no-web", action="store_true", help="run without the web UI")
    run_p.set_defaults(func=_cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
