"""Command-line entry point: ``tvastr demo`` / ``tvastr serve`` / ``tvastr version``."""

from __future__ import annotations

import argparse
import sys

from tvastr import __version__
from tvastr.config import get_settings
from tvastr.logging import configure_logging


def _run_demo() -> int:
    from tvastr.pipeline import build_pipeline

    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    print("\n=== tvastr demo — Autonomous Code Remediation Agent ===")
    print(
        f"mode: {'MOCK (offline)' if settings.use_mocks else 'LIVE'}  "
        f"| target repo: {settings.github_repo}\n"
    )

    run = build_pipeline(settings).run()

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
        if o.pull_request_url:
            print(f"    pull request: {o.pull_request_url}")
        if o.routing:
            print("    routing     :")
            for d in o.routing:
                print(f"        - {d['task']:<16} → {d['target']:<5} ({d['model']})")
                print(f"          {d['reason']}")
        print()

    print("─" * 70)
    print("Audit trail recorded. In live mode these would be indexed in OpenSearch.")
    return 0


def _run_serve(host: str, port: int) -> int:
    import uvicorn

    uvicorn.run("tvastr.api.app:create_app", factory=True, host=host, port=port, reload=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tvastr", description="Autonomous Code Remediation Agent")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("demo", help="Run the pipeline against the bundled sample logs (mock mode)")
    sub.add_parser("version", help="Print the version")

    serve = sub.add_parser("serve", help="Start the FastAPI server")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)

    if args.command == "demo":
        return _run_demo()
    if args.command == "serve":
        return _run_serve(args.host, args.port)
    if args.command == "version":
        print(f"tvastr {__version__}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
