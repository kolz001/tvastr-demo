"""Command-line entry point.

Subcommands:
  demo                  Run the pipeline against a JSONL log file or stdin.
  serve                 Start the FastAPI server.
  ingest-github-issues  Harvest a repo's bug-labeled issues into a LogEvent JSONL.
  ingest-loki           Harvest a Grafana Loki LogQL query into a LogEvent JSONL.
  version               Print the version.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tvastr import __version__
from tvastr.config import get_settings
from tvastr.logging import configure_logging

if TYPE_CHECKING:
    from tvastr.ingestion import LogSource


def _build_log_source(logs_arg: str | None) -> LogSource:
    """Pick a log source from the ``--logs`` value: ``-`` = stdin, path = file."""
    from tvastr.ingestion import SimulatedLogSource, StdinLogSource

    if logs_arg == "-":
        return StdinLogSource()
    if logs_arg:
        return SimulatedLogSource(Path(logs_arg))
    return SimulatedLogSource()


def _source_label(source: LogSource) -> str:
    return source.name if source.name == "stdin" else str(getattr(source, "path", source.name))


def _run_demo(logs_arg: str | None, dry_run: bool) -> int:
    from tvastr.pipeline import build_pipeline

    settings = get_settings()
    if dry_run:
        settings = settings.model_copy(update={"dry_run": True})
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    source = _build_log_source(logs_arg)
    mode_label = "MOCK (offline)" if settings.use_mocks else "LIVE"
    if settings.dry_run:
        mode_label += " · DRY-RUN (no PR will be opened)"
    print("\n=== tvastr demo — Autonomous Code Remediation Agent ===")
    print(
        f"mode: {mode_label}  "
        f"| target repo: {settings.github_repo}  "
        f"| logs: {_source_label(source)}  "
        f"| audit: {settings.audit_backend}\n"
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


def _run_ingest_loki(
    url: str | None, query: str | None, out: Path, *, limit: int, since_hours: int
) -> int:
    from tvastr.ingestion import harvest_loki_to_jsonl

    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    url = url or settings.loki_url
    query = query or settings.loki_query
    if not query:
        print(
            "error: --query (or TVASTR_LOKI_QUERY) is required, "
            'e.g. \'{app="myapp"} |= "Error"\'.',
            file=sys.stderr,
        )
        return 2

    entries, events = harvest_loki_to_jsonl(
        url=url,
        query=query,
        out_path=out,
        user=settings.loki_user,
        password=settings.loki_password,
        limit=limit,
        since_hours=since_hours,
        use_mocks=settings.use_mocks,
    )

    mode = (
        "MOCK (offline)"
        if settings.use_mocks or not url
        else "LIVE (Loki HTTP API)"
    )
    print(
        f"\nHarvested {entries} entries from Loki "
        f"(url={url or '-'}, query={query!r}, since={since_hours}h, limit={limit})\n"
        f"  → wrote {events} LogEvent(s) to {out}\n"
        f"  mode: {mode}\n"
        f"\nNext: run the demo against the new file:\n"
        f"  uv run python -m tvastr.cli demo --logs {out} --dry-run\n"
    )
    return 0 if events > 0 else 1


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

    demo = sub.add_parser("demo", help="Run the pipeline against a JSONL log file or stdin")
    demo.add_argument(
        "--logs",
        default=None,
        help=(
            "Path to a JSONL file of LogEvents, or '-' to read from stdin. "
            "Default: bundled LlamaIndex samples."
        ),
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

    loki = sub.add_parser(
        "ingest-loki",
        help="Harvest a Grafana Loki LogQL query into a LogEvent JSONL",
    )
    loki.add_argument(
        "--url",
        default=None,
        help="Loki base URL (default: TVASTR_LOKI_URL, e.g. http://localhost:3100)",
    )
    loki.add_argument(
        "--query",
        default=None,
        help='LogQL query (default: TVASTR_LOKI_QUERY, e.g. \'{app="myapp"} |= "Error"\')',
    )
    loki.add_argument(
        "--out",
        type=Path,
        default=Path("data/sample_logs/loki.jsonl"),
        help="Output JSONL path",
    )
    loki.add_argument(
        "--limit", type=int, default=500, help="Max log entries to read (default: 500)"
    )
    loki.add_argument(
        "--since-hours",
        type=int,
        default=24,
        help="How many hours of history to query (default: 24)",
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
    if args.command == "ingest-loki":
        return _run_ingest_loki(
            args.url, args.query, args.out, limit=args.limit, since_hours=args.since_hours
        )
    if args.command == "version":
        print(f"tvastr {__version__}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
