"""Command line entry point: pbi-doctor login | scan | triage | demo."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from importlib import resources
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .backends import BackendError, make_backend
from .config import load_config, load_targets
from .crawler import crawl
from .discovery import expand_targets
from .models import ScanResult
from .report import write_outputs
from .triage import triage_visuals


def _backend_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    kw: dict[str, Any] = {}
    if getattr(args, "model", None):
        kw["model"] = args.model
    if getattr(args, "base_url", None):
        kw["base_url"] = args.base_url
    return kw


async def _triage_and_report(scan: ScanResult, cfg: dict[str, Any], args: argparse.Namespace) -> int:
    backend = make_backend(args.backend, **_backend_kwargs(args))
    findings = await triage_visuals(scan.visuals, backend, cfg)
    threshold = float(cfg.get("triage", {}).get("review_threshold", 0.6))
    paths = write_outputs(args.out, scan, findings, backend.name, threshold)
    broken = sum(1 for f in findings if f.is_confirmed_broken)
    print()
    print(f"Visuals checked: {len(scan.visuals)}  |  broken: {broken}  |  pages not scanned: {len(scan.page_errors)}")
    print(f"Report: {paths['html'].resolve()}")
    print(f"Data:   {paths['json'].resolve()}  and  {paths['csv'].name}")
    if args.fail_on_broken and (broken or scan.page_errors):
        return 1
    return 0


async def _scan(args: argparse.Namespace, cfg: dict[str, Any], targets: dict[str, Any]) -> int:
    token = os.environ.get("PBI_ACCESS_TOKEN")
    reports = await expand_targets(targets, token)
    if not reports:
        print("No reports to scan. Check your targets file.", file=sys.stderr)
        return 2
    storage = args.session if args.session and Path(args.session).exists() else None
    if args.session and not storage and not args.no_session_ok:
        print(f"Note: no saved session at {args.session}; scanning without sign-in.")
    print(f"Scanning {len(reports)} report(s)...")
    scan = await crawl(reports, cfg, storage_state=storage, headed=args.headed)
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / "scan.json").write_text(json.dumps(scan.to_dict(), indent=2), encoding="utf-8")
    return await _triage_and_report(scan, cfg, args)


def cmd_scan(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.concurrency:
        cfg["timing"]["concurrency"] = args.concurrency
    targets = load_targets(args.targets)
    return asyncio.run(_scan(args, cfg, targets))


def cmd_triage(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    scan = ScanResult.from_dict(json.loads(Path(args.input).read_text(encoding="utf-8")))
    return asyncio.run(_triage_and_report(scan, cfg, args))


def cmd_demo(args: argparse.Namespace) -> int:
    demo_dir = resources.files("pbi_visual_doctor").joinpath("demo")
    targets = load_targets(Path(str(demo_dir.joinpath("targets.yaml"))))
    cfg = load_config(overrides={"timing": {"render_settle_ms": 800, "render_max_wait_ms": 5000, "concurrency": 4}})
    args.session = None
    args.no_session_ok = True
    print("Running against the bundled demo pages (fake Power BI markup, no sign-in needed).")
    return asyncio.run(_scan(args, cfg, targets))


def cmd_login(args: argparse.Namespace) -> int:
    from .auth import login

    login(args.session, args.start_url)
    return 0


def _add_triage_opts(p: argparse.ArgumentParser, default_out: str) -> None:
    p.add_argument("--backend", default=os.environ.get("PBI_DOCTOR_BACKEND", "rules"),
                   help="jev | laya | laya-http | rules (default: rules, or $PBI_DOCTOR_BACKEND)")
    p.add_argument("--model", help="Model to request, e.g. jev-1.13.0, or english|multilingual for Laya")
    p.add_argument("--base-url", help="Override the Jev or laya-serve base URL")
    p.add_argument("--out", default=default_out, help=f"Output folder (default: {default_out})")
    p.add_argument("--config", help="YAML file overriding selectors, phrases or timings")
    p.add_argument("--fail-on-broken", action="store_true", help="Exit with code 1 if anything is broken (for CI)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pbi-doctor",
        description="Find broken visuals in published Power BI reports and triage them with Jev or Laya.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="Sign in once in a browser window and save the session")
    p.add_argument("--session", default=".auth/state.json")
    p.add_argument("--start-url", default="https://app.powerbi.com/home")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("scan", help="Crawl the reports in a targets file, triage problems, write the report")
    p.add_argument("--targets", required=True, help="YAML file listing reports, pages or workspaces")
    p.add_argument("--session", default=".auth/state.json", help="Saved session from 'pbi-doctor login'")
    p.add_argument("--headed", action="store_true", help="Show the browser while scanning")
    p.add_argument("--concurrency", type=int, help="Pages scanned in parallel")
    p.set_defaults(func=cmd_scan, no_session_ok=False)
    _add_triage_opts(p, "pbi-doctor-report")

    p = sub.add_parser("triage", help="Re-run triage on a saved scan.json (e.g. to compare backends)")
    p.add_argument("--input", required=True, help="scan.json from a previous scan")
    p.set_defaults(func=cmd_triage)
    _add_triage_opts(p, "pbi-doctor-report")

    p = sub.add_parser("demo", help="Run end to end on bundled fake reports")
    p.add_argument("--headed", action="store_true")
    p.set_defaults(func=cmd_demo)
    _add_triage_opts(p, "pbi-doctor-demo")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except BackendError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
