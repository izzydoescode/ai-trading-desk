# Build verification — October 28, 2026

Implemented and locally executed on Python 3.14.0:

- Complete LangGraph workflow and CLI.
- 35 passing tests, including numerical reference values, malformed data/model responses, source failures, abstention, dissent, numeric overflow, and CLI artifacts.
- Ruff lint and formatting checks.
- Credential-free trend, conflict, and missing-data reports.
- Current-time stale-fixture abstention.
- Source distribution and wheel packaging, with bundled fixture assets.

Locked direct runtime versions: LangGraph 1.2.18, Pydantic 2.14.1, httpx 0.28.1. Development versions: pytest 9.2.0, pytest-asyncio 1.4.1, Ruff 0.17.3. `uv.lock` captures the complete resolution; uv 0.13.6 was used locally.

CI is configured for Python 3.11–3.14 but has not run on GitHub. The local Python 3.14 test run reports 13 upstream LangSmith deprecation warnings about `asyncio.iscoroutinefunction`; tests pass. Warnings are not suppressed.

## Honest verification boundary

OpenAI and Alpha Vantage adapters have HTTP contract tests and injected failure tests. No authenticated external market-data call or paid LLM call has been executed. Included reports are deterministic rules over explicitly synthetic data. They are not historical AAPL prices, a live LLM transcript, a backtest, or evidence of predictive performance.

## Publication status

A connected GitHub repository listing and `bagHolderPro/package.json` were inspected. The existing dashboard uses React, TypeScript, and ApexCharts. This Python backend has no dependency on it.

The connected tools do not expose repository creation. The local GitHub CLI reports an invalid credential. The project is ready to publish after authenticating the intended GitHub account; no public repository URL is claimed. Run the publishing commands in README.md from this directory.

The Albert job URL supplied in the brief could not be retrieved by the browsing tool. No job-specific requirements beyond the supplied brief are asserted, and no résumé or recruiter message has been sent.
