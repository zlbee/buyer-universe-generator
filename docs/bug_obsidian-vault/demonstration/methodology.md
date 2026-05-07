# Preface
This project is relatively complex. Because I was not familiar with the M&A domain and both time and token budget were limited, the full project has not been completely implemented. The current implementation has reached a meaningful intermediate milestone: it can build a seller profile and recall different types of potential buyers from multiple data sources. However, the later analysis-based filtering steps have not yet been implemented, so the methodology for those later steps remains at the design stage.

# Global Design Principles
- External data-source calls and LLM calls both have time and monetary costs, so each intermediate result should be cached to avoid unnecessary repeated work.
- Every LLM query and raw data-source query should be saved with an audit record to support later tuning and debugging.
- The system design must prioritize high extensibility, high cohesion, and low coupling.

# TargetProfile Builder
1. Principles
	- A clear seller profile is important because it provides the anchor for downstream analysis-based filtering.
	- Building the seller profile requires multiple data sources so the system can assemble all attributes needed by later retrieval and scoring stages.

2. Core domain fields
Core fields: `name`, `ticker`, `cik`, `exchange`, `sic`
Business description fields: `business_summary`, `products`, `customer_segments`, `channels`, `geographies`, `adjacent_categories`
Financial scale fields: `size_metrics`
Keyword fields: `keywords`, `keyword_groups`
Evidence fields: `feature_labels`, `feature_evidence`

3. Multi-source data recall
The source plan follows `backend/config/retrieval_rules.yaml`.

- SEC EDGAR is the primary source for target identity, filings, business description, and strategy evidence.
- Polygon.io, official company/IR pages, and recent news are optional enrichment sources for profile, scale, product, geography, and current context.
- LLM extraction converts collected source text into a structured `TargetProfile`; it is not treated as a standalone factual source.
- Source documents, evidence, labels, and final profiles are cached so repeated runs can reuse stable results.

# Potential Buyer Recaller
1. Principles
	- The main task of this stage is to recall potential buyers that may acquire the seller for any reason. It therefore runs recall from multiple dimensions and perspectives. Any buyer that satisfies any recall condition can be included.
	- This stage values recall more than precision. The goal is to avoid missing plausible buyers; precision-improving filters run later.
	- The key design point is to use different data sources, rules, and parameters for each retrieval dimension.

2. Sub-retriever overview
	- Same-industry retriever
	- Same-industry historical M&A retriever
	- Strategic acquisition intent retriever
	- PE historical deal activity retriever

3. Current retriever design and data sources
- `SameSicRetriever`
	- Goal: recall public strategic buyers whose SEC SIC matches the target's SIC.
	- Method: use the `public_company_peer_discovery` use case and EDGAR public-company metadata. The retriever performs light filtering only: it excludes the target itself, unnamed candidates, and candidates without same-SIC evidence.
	- Data source: EDGAR is the configured source. This provides strong evidence because SIC is part of the official public-company disclosure system and is more reliable than broad web classification for US-listed companies.

- `MAHistoryRetriever`
	- Goal: recall strategic buyers that have recently acquired companies in the same or adjacent sector.
	- Method: derive transaction queries from the target profile's company keywords, products, customer segments, channels, adjacent categories, and configured transaction terms. Keep only same-sector or adjacent-sector events, aggregate repeated events by buyer, and preserve structured deal metadata.
	- Data sources: EDGAR, NewsAPI, and Google News RSS. EDGAR is the primary source for transaction signals. The configured SEC form type is `8-K`; Item `2.01` is the primary completed-acquisition signal, while Item `1.01` is retained as supporting agreement evidence. NewsAPI and Google News RSS supplement EDGAR for deals that may not be captured cleanly in filings.
	- Parameters: the configured lookback is 5 years, with caps on companies, documents, queries, and page size. EDGAR filing text is fetched at the primary item-section scope when available.

- `StrategicAcquisitionIntentRetriever`
	- Goal: recall strategic buyers that have publicly signaled M&A appetite, corporate-development focus, roll-up strategy, or acquisition interest in the target's sector.
	- Method: use LLM web search to find and classify public evidence of acquisition intent. It limits candidates and evidence per candidate so web-search spending remains bounded.
	- Data source: LLM's web-search tool, configured as `llm_web_search`. It is intentionally assigned lower evidence strength because the signal is extracted from cited web results rather than from a fully controlled primary-source pipeline.
	- Parameters: the configured lookback is 1 year, with caps on candidates, documents, queries, and evidence per candidate. Eligible sector matches are `same` and `adjacent`.

- `PEDealActivityRetriever`
	- Goal: recall financial sponsors with recent same-sector or adjacent-sector deal activity.
	- Method: generate industry query terms from SIC taxonomy, target products, keywords, adjacent categories, and the target name. Search M&A records, keep only acquirers that exactly match a configured PE canonical name or alias, filter out old/unrelated/malformed records, deduplicate repeated deal records, and preserve deal events.
	- Data sources: LLM web search is the primary source for sponsor activity. The PE seed universe in `backend/config/pe_seed_universe.yaml` is only an identity filter; a candidate still needs recent relevant deal evidence before it is emitted. Financial Modeling Prep is the secondary configured source for M&A records, but it requires further tuning.
	- Parameters: the configured lookback is 5 years. The retriever also has caps on documents, queries, page size, candidates, scanned seed firms, and web-search batch size. Eligible sector matches are `same` and `adjacent`.

# Unified Retrieval Rule Configuration
1. Principles
	- Because the system retrieves from multiple data sources, and each source has its own availability, cost, cache behavior, and reliability profile, source configuration should live in one unified place.

2. Configuration method
`backend/config/retrieval_rules.yaml` is organized into two cooperating layers.

- `providers`
	- Defines provider availability and provider-specific retrieval knobs.

- `stages`
	- Binds pipeline stages to use cases, and use cases to source IDs, source roles, source priority, source dimensions, and source-strength levels.
	- The nested `retrievers` section maps concrete retriever classes to use cases and retrieval parameters such as target-profile SEC form preference, LLM extraction limits, size metric keys, lookback windows, candidate caps, query caps, eligible sector matches, transaction terms, and EDGAR form/item settings.

In short, `providers` defines what sources are available and their knobs, while `stages` defines where sources are used and how their evidence should be trusted in that use case.

Together these layers let the code ask the `DataSourceStrategy` for the selected source and retriever policy, instead of hardcoding provider switches, source strength, or retrieval parameters inside each retriever.

# Potential Buyer Hard Filter
This section is still primarily a design-stage component. The intended role is to remove candidates that clearly fail minimum acquisition conditions after candidate expansion and deduplication.

1. Principles
	- Use hard conditions to filter out companies that are obviously unsuitable acquirers.
	- Preserve recall during raw retrieval, then apply these hard filters after candidate normalization and deduplication.
	- Record every exclusion or pending-verification decision with machine-readable reasons.

2. Designed filter dimensions
- Self-candidate exclusion: the target company must never appear as its own buyer.
- Capacity requirement: buyers should have enough financial and operational capacity to acquire the target.
	- buyer_market_cap > 1.5 * seller_market_cap
	- buyer_revenue >= seller_revenue
	- buyer_cash >= 0.5 * seller_valuation
	- buyer_debt_to_equity <= 1
- Initial evidence score: each buyer is deduplicated and counted across retriever outputs. Candidates score higher when they appear more often, appear across more retrieval dimensions, or come from stronger-evidence sources; candidates below a configured threshold are excluded.

# Potential Buyer Analytical Filter
This section is also mainly design-stage methodology. Its purpose is to rank retained buyers by acquisition likelihood, explain the main deal rationale, and produce the final buyer universe. It sits after raw candidate recall, normalization, deduplication, and hard filtering.

1. Principles
	- Build a multi-source profile for each retained buyer, then compare it against the seller profile from the perspective of acquisition likelihood.
	- Use a hybrid scoring method that combines rule-based signals, source evidence, and LLM-assisted classification or explanation.
	- Keep scoring rules in a unified configuration file so weights and thresholds can be tuned without changing retriever code.
	- Preserve source paths, citations, fit reasons, risk flags, and score components so the final buyer universe remains explainable.

2. Implementation technology choice
	- The intended approach is similar to a RAG reranking pipeline: based on a broad candidate pool, candidates are reranked using a combination of term-frequency/BM25-style matching, profile embedding similarity, structured rule scores, and LLM scoring across deal drivers such as strategic fit, historical M&A activity, financial capacity, and synergistic potential.
