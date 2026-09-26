"""Command line entry point: pbi-doctor login | scan | triage | demo."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .backends import BackendError, make_backend
from .config import load_config, load_targets
from .crawler import crawl
from .discovery import DiscoveryError, discover, expand_targets, token_from_session
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


def _needs_token(targets: dict[str, Any]) -> bool:
    return bool(targets.get("workspaces")) or any(not r.get("pages") for r in targets.get("reports") or [])


async def _find_reports(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Reports to scan: from the targets file if given, else discovered from the signed-in account."""
    targets = load_targets(args.targets) if args.targets else None
    token = os.environ.get("PBI_ACCESS_TOKEN") or None
    session = args.session if args.session and Path(args.session).exists() else None
    if not token and session and (targets is None or _needs_token(targets)):
        print("Reading your Power BI workspaces...")
        token = await token_from_session(session, headed=args.headed)
    if targets is not None:
        return await expand_targets(targets, token)
    if not token:
        raise DiscoveryError("You are not signed in. Run 'pbi-doctor login' first.")
    return await discover(
        token,
        workspaces=args.workspace,
        reports=args.report,
        urls=args.url,
        include_personal=args.include_my_workspace,
        include_hidden=args.include_hidden_pages,
    )


def _print_plan(reports: list[dict[str, Any]]) -> None:
    pages = sum(len(r.get("pages") or []) for r in reports)
    print(f"Found {len(reports)} report(s), {pages} page(s):")
    for r in reports:
        n = len(r.get("pages") or [])
        detail = f"{n} page(s)" if n else "(pages found by clicking tabs)"
        print(f"  {r.get('name') or r.get('url')}  {detail}")


async def _scan(args: argparse.Namespace, cfg: dict[str, Any], reports: list[dict[str, Any]]) -> int:
    if not reports:
        print("No reports to scan. Check your filters or targets file.", file=sys.stderr)
        return 2
    _print_plan(reports)
    if getattr(args, "list", False):
        return 0
    storage = args.session if args.session and Path(args.session).exists() else None
    if args.session and not storage and not args.no_session_ok:
        print(f"Note: no saved session at {args.session}; scanning without sign-in.")
    print(f"Scanning {len(reports)} report(s)...")
    scan = await crawl(reports, cfg, storage_state=storage, headed=args.headed,
                       include_hidden=getattr(args, "include_hidden_pages", False))
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / "scan.json").write_text(json.dumps(scan.to_dict(), indent=2), encoding="utf-8")
    return await _triage_and_report(scan, cfg, args)


RUNS_DIR = "pbi-doctor-report"


def _run_folder(args: argparse.Namespace, suffix: str = "") -> str:
    """A new folder per run, e.g. pbi-doctor-report/2026-09-26_21-38-05_laya, so results are kept."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    base = Path(RUNS_DIR) / f"{stamp}_{args.backend}{suffix}"
    out, n = base, 2
    while out.exists():
        out = base.with_name(f"{base.name}-{n}")
        n += 1
    return str(out)


def cmd_scan(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if not args.out:
        args.out = _run_folder(args)
    if args.concurrency:
        cfg["timing"]["concurrency"] = args.concurrency

    async def run() -> int:
        return await _scan(args, cfg, await _find_reports(args))

    return asyncio.run(run())


def cmd_triage(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    raw = Path(args.input).read_text(encoding="utf-8")
    scan = ScanResult.from_dict(json.loads(raw))
    if not args.out:
        args.out = _run_folder(args, "_triage")
        # Keep the crawl alongside the new findings so the folder is self-contained.
        Path(args.out).mkdir(parents=True, exist_ok=True)
        (Path(args.out) / "scan.json").write_text(raw, encoding="utf-8")
    return asyncio.run(_triage_and_report(scan, cfg, args))


def cmd_demo(args: argparse.Namespace) -> int:
    demo_dir = resources.files("pbi_visual_doctor").joinpath("demo")
    targets = load_targets(Path(str(demo_dir.joinpath("targets.yaml"))))
    cfg = load_config(overrides={"timing": {"render_settle_ms": 800, "render_max_wait_ms": 5000, "concurrency": 4}})
    args.session = None
    args.no_session_ok = True
    print("Running against the bundled demo pages (fake Power BI markup, no sign-in needed).")
    return asyncio.run(_scan(args, cfg, targets.get("reports") or []))


def cmd_login(args: argparse.Namespace) -> int:
    from .auth import login

    login(args.session, args.start_url)
    print("Next: pbi-doctor scan")
    return 0


def _add_triage_opts(p: argparse.ArgumentParser, default_out: Optional[str]) -> None:
    p.add_argument("--backend", default=os.environ.get("PBI_DOCTOR_BACKEND", "rules"),
                   help="jev | laya | laya-http | rules (default: rules, or $PBI_DOCTOR_BACKEND)")
    p.add_argument("--model", help="Model to request, e.g. jev-1.13.0, or english|multilingual for Laya")
    p.add_argument("--base-url", help="Override the Jev or laya-serve base URL")
    out_help = f"Output folder (default: {default_out})" if default_out else (
        f"Output folder (default: a new timestamped folder inside {RUNS_DIR}/)")
    p.add_argument("--out", default=default_out, help=out_help)
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

    p = sub.add_parser(
        "scan",
        help="Find your reports, crawl them, triage problems, write the report",
        description="With no options, scans every report in every workspace you can access.",
    )
    p.add_argument("--workspace", action="append", metavar="NAME",
                   help="Only this workspace (name, part of the name, or id). Repeatable")
    p.add_argument("--report", action="append", metavar="NAME",
                   help="Only reports whose name contains this text. Repeatable")
    p.add_argument("--url", action="append", metavar="URL",
                   help="A report or workspace URL copied from the browser. Repeatable")
    p.add_argument("--include-my-workspace", action="store_true", help="Also scan reports in My workspace")
    p.add_argument("--include-hidden-pages", action="store_true",
                   help="Also scan hidden pages (drill-through, tooltip); they are labelled in the report")
    p.add_argument("--list", action="store_true", help="Print what would be scanned, then exit")
    p.add_argument("--targets", help="Advanced: YAML file listing reports, pages or workspaces")
    p.add_argument("--session", default=".auth/state.json", help="Saved session from 'pbi-doctor login'")
    p.add_argument("--headed", action="store_true", help="Show the browser while scanning")
    p.add_argument("--concurrency", type=int, help="Pages scanned in parallel")
    p.set_defaults(func=cmd_scan, no_session_ok=False)
    _add_triage_opts(p, None)

    p = sub.add_parser("triage", help="Re-run triage on a saved scan.json (e.g. to compare backends)")
    p.add_argument("--input", required=True, help="scan.json from a previous scan")
    p.set_defaults(func=cmd_triage)
    _add_triage_opts(p, None)

    p = sub.add_parser("demo", help="Run end to end on bundled fake reports")
    p.add_argument("--headed", action="store_true")
    p.set_defaults(func=cmd_demo)
    _add_triage_opts(p, "pbi-doctor-demo")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (BackendError, DiscoveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
