"""Command-line entry point.

Subcommands:
  demo                  Run the pipeline against a JSONL log file (default: bundled samples).
  serve                 Start the FastAPI server.
  ingest-github-issues  Harvest a repo's bug-labeled issues into a LogEvent JSONL.
  version               Print the version.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tvastr import __version__
from tvastr.config import get_settings
from tvastr.logging import configure_logging


def _run_demo(logs_path: Path | None, dry_run: bool) -> int:
    from tvastr.ingestion import SimulatedLogSource
    from tvastr.pipeline import build_pipeline

    settings = get_settings()
    if dry_run:
        settings = settings.model_copy(update={"dry_run": True})
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    source = SimulatedLogSource(logs_path) if logs_path else SimulatedLogSource()
    mode_label = "MOCK (offline)" if settings.use_mocks else "LIVE"
    if settings.dry_run:
        mode_label += " · DRY-RUN (no PR will be opened)"
    print("\n=== tvastr demo — Autonomous Code Remediation Agent ===")
    print(
        f"mode: {mode_label}  "
        f"| target repo: {settings.github_repo}  "
        f"| logs: {source.path}\n"
    )

    run = build_pipeline(settings, log_source=source).run()

    print("─" * 70)
    print(
        f"Ingested {run.events_ingested} events → "
        f"{run.patterns_detected} distinct patterns → "
        f"{run.patterns_selected} above recurrence threshold "
        f"({settings.recurrence_threshold}x)\n"
    )

    if not run.outcomes:
        print("No patterns met the remediation threshold.")
        return 0

    for i, o in enumerate(run.outcomes, start=1):
        print(f"[{i}] {o.title}")
        print(f"    occurrences : {o.count}   sensitivity: {o.sensitivity}")
        print(f"    outcome     : {o.outcome}")
        if o.outcome == "dry_run":
            print(f"    would-open  : {o.pr_title or '(no draft)'}")
            print(f"    branch      : {o.pr_branch}")
            if o.pr_changes:
                print("    proposed changes:")
                for change in o.pr_changes:
                    print(f"        path     : {change['path']}")
                    if change.get("rationale"):
                        rationale = change["rationale"].splitlines()[0][:200]
                        print(f"        rationale: {rationale}")
                    diff = change.get("diff") or ""
                    if diff.strip():
                        print("        diff     :")
                        for line in diff.splitlines()[:40]:
                            print(f"          {line}")
                        if len(diff.splitlines()) > 40:
                            print(f"          … ({len(diff.splitlines()) - 40} more lines)")
                    print()
        elif o.pull_request_url:
            print(f"    pull request: {o.pull_request_url}")
        if o.routing:
            print("    routing     :")
            for d in o.routing:
                print(f"        - {d['task']:<16} → {d['target']:<5} ({d['model']})")
                print(f"          {d['reason']}")
        print()

    print("─" * 70)
    if settings.dry_run:
        print("Dry-run: no PRs were opened. Re-run without --dry-run to go live.")
    else:
        print("Audit trail recorded. In live mode these would be indexed in OpenSearch.")
    return 0


def _run_serve(host: str, port: int) -> int:
    import uvicorn

    uvicorn.run("tvastr.api.app:create_app", factory=True, host=host, port=port, reload=False)
    return 0


def _run_ingest_github_issues(
    repo: str, out: Path, *, label: str, limit: int, service: str | None
) -> int:
    from tvastr.ingestion import harvest_issues_to_jsonl

    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    issues, events = harvest_issues_to_jsonl(
        repo=repo,
        out_path=out,
        token=settings.github_token,
        label=label,
        limit=limit,
        use_mocks=settings.use_mocks,
        default_service=service,
    )

    print(
        f"\nHarvested {issues} issues from {repo} (label={label}, limit={limit})\n"
        f"  → wrote {events} LogEvent(s) to {out}\n"
        f"  mode: {'MOCK (offline)' if settings.use_mocks else 'LIVE (GitHub API)'}\n"
        f"\nNext: run the demo against the new file:\n"
        f"  uv run python -m tvastr.cli demo --logs {out}\n"
    )
    return 0 if events > 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tvastr", description="Autonomous Code Remediation Agent")
    sub = parser.add_subparsers(dest="command")

    demo = sub.add_parser("demo", help="Run the pipeline against a JSONL log file")
    demo.add_argument(
        "--logs",
        type=Path,
        default=None,
        help="Path to a JSONL file of LogEvents (default: bundled Haystack samples)",
    )
    demo.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full agent flow but skip PR creation; print the proposed draft.",
    )

    sub.add_parser("version", help="Print the version")

    serve = sub.add_parser("serve", help="Start the FastAPI server")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)

    ingest = sub.add_parser(
        "ingest-github-issues",
        help="Harvest bug-labeled issues from a GitHub repo into a LogEvent JSONL",
    )
    ingest.add_argument(
        "--repo",
        default="run-llama/llama_index",
        help="owner/repo (default: run-llama/llama_index)",
    )
    ingest.add_argument(
        "--out",
        type=Path,
        default=Path("data/sample_logs/github_issues.jsonl"),
        help="Output JSONL path",
    )
    ingest.add_argument("--label", default="bug", help="Issue label to filter on (default: bug)")
    ingest.add_argument("--limit", type=int, default=50, help="Max issues to read (default: 50)")
    ingest.add_argument(
        "--service",
        default=None,
        help="Service name to stamp on events (default: repo name)",
    )

    args = parser.parse_args(argv)

    if args.command == "demo":
        return _run_demo(args.logs, args.dry_run)
    if args.command == "serve":
        return _run_serve(args.host, args.port)
    if args.command == "ingest-github-issues":
        return _run_ingest_github_issues(
            args.repo, args.out, label=args.label, limit=args.limit, service=args.service
        )
    if args.command == "version":
        print(f"tvastr {__version__}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
