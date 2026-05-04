# AGENTS.md

## MUST FOLLOW
- Add clear comments in English to explain non-obvious logic, key decisions, complex algorithms, or public interfaces.
- Documentation should be written in English.
- When implementing retrieval or processing for raw data sources, check and honor the applicable configuration in `backend/config/datasources.yaml`.

## Raw Data Source Documentation
- SEC EDGAR APIs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- EdgarTools filings guide: https://edgartools.readthedocs.io/en/latest/guides/working-with-filing/
- EdgarTools Filing API: https://edgartools.readthedocs.io/en/latest/api/filing/
- EdgarTools data objects: https://edgartools.readthedocs.io/en/latest/data-objects/
- Polygon.io ticker overview: https://polygon.io/docs/rest/stocks/tickers/ticker-overview
- Financial Modeling Prep API documentation: https://site.financialmodelingprep.com/developer/docs
- NewsAPI Everything endpoint: https://newsapi.org/docs/endpoints/everything
- Google News RSS endpoint: https://news.google.com/rss
- OpenRouter chat completion API: https://openrouter.ai/docs/api-reference/chat-completion
- OpenRouter web search server tool: https://openrouter.ai/docs/guides/features/server-tools/web-search
- Official company and investor-relations pages: no single provider API; use each target company's public website documentation or robots/policy pages when available.
