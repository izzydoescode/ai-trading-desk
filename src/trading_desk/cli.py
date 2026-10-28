"""CLI with explicit data/reasoning modes and machine-readable stdout."""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx

from trading_desk.graph import run_research
from trading_desk.llm import OpenAIProvider
from trading_desk.models import Report
from trading_desk.tools.market_data import AlphaVantageProvider, FixtureProvider


def markdown(report: Report) -> str:
    i = report.indicators
    lines = [
        f"# {report.symbol} — {report.classification}",
        "",
        f"Mode: {report.mode} | Source: {report.data.source} | Synthetic: {report.data.synthetic}",
        f"Evaluated as of: {report.evaluated_as_of.isoformat()}",
        f"Market data timestamp: {report.market_data_timestamp}",
        f"Abstained: {report.abstained}",
        "",
        "## Measured observations",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    lines += [f"| {k} | {v} |" for k, v in i.model_dump().items()]
    for name in ("technical", "contrarian", "synthesis"):
        analysis = getattr(report, name)
        lines += ["", f"## {name.title()}", "", analysis.summary, ""]
        lines += [f"- {r}" for r in analysis.risks]
    lines += ["", "## Limitations", ""] + [f"- {x}" for x in report.limitations]
    lines += ["", "## Dissent", ""] + [f"- {x}" for x in report.dissent]
    lines += [
        "",
        "## Execution trace",
        "",
        "| Step | Status | Provider | ms |",
        "|---|---|---|---:|",
    ]
    lines += [f"| {t.agent} | {t.status} | {t.provider} | {t.duration_ms} |" for t in report.trace]
    return "\n".join(lines + ["", report.purpose, ""])


async def execute(args) -> Report:
    async with httpx.AsyncClient() as client:
        if args.source == "alpha-vantage":
            key = os.environ.get("ALPHAVANTAGE_API_KEY")
            if not key:
                raise ValueError("Export ALPHAVANTAGE_API_KEY for alpha-vantage data")
            provider = AlphaVantageProvider(key, client)
        else:
            provider = FixtureProvider(args.scenario)
        llm = None
        if args.mode == "llm":
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                raise ValueError(
                    "Export OPENAI_API_KEY for LLM mode; no silent simulation fallback"
                )
            llm = OpenAIProvider(key, os.environ.get("OPENAI_MODEL", "gpt-4o-mini"), client)
        as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00")) if args.as_of else None
        return await run_research(args.symbol, provider, llm, as_of)


def main() -> int:
    parser = argparse.ArgumentParser(description="Research-only market intelligence desk")
    sub = parser.add_subparsers(dest="command", required=True)
    analyze = sub.add_parser("analyze")
    analyze.add_argument("symbol")
    analyze.add_argument("--source", choices=["fixture", "alpha-vantage"], default="fixture")
    analyze.add_argument("--scenario", choices=["trend", "conflict", "missing"], default="trend")
    analyze.add_argument("--mode", choices=["deterministic", "llm"], default="deterministic")
    analyze.add_argument("--as-of", help="Timezone-aware historical evaluation timestamp")
    analyze.add_argument("--output", type=Path, help="JSON output path (stdout if omitted)")
    analyze.add_argument("--markdown", type=Path, help="Optional Markdown output path")
    analyze.add_argument("--verbose", action="store_true", help="JSON node logs on stderr")
    args = parser.parse_args()
    if args.source != "fixture" and args.scenario != "trend":
        parser.error("--scenario applies only to fixture data")
    if args.verbose:
        logging.basicConfig(level=logging.WARNING, format="%(message)s")
        logging.getLogger("trading_desk").setLevel(logging.INFO)
    print(f"Data={args.source}; reasoning={args.mode}. Research only.", file=sys.stderr)
    try:
        report = asyncio.run(execute(args))
    except (ValueError, OSError) as exc:
        print(f"Configuration/output error: {exc}", file=sys.stderr)
        return 2
    content = report.model_dump_json(indent=2) + "\n"
    try:
        if args.output:
            args.output.write_text(content)
        else:
            print(content, end="")
        if args.markdown:
            args.markdown.write_text(markdown(report))
    except OSError:
        print("Could not write report output", file=sys.stderr)
        return 2
    print(f"Classification={report.classification}; abstained={report.abstained}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
