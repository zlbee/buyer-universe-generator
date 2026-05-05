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
The current source plan is driven mainly by `backend/config/retrieval_rules.yaml`.

- SEC EDGAR is the required primary source. It is used for target identity resolution, filing metadata, business description evidence, and company strategy evidence. The implementation fetches recent `10-K`, `10-Q`, `8-K`, `S-1`, and `S-1/A` filings. For business description extraction, `10-K` is preferred first and `10-Q` is used as fallback. For company strategy extraction, the preferred order is `10-K`, `8-K`, `S-1`/`S-1/A`, then `10-Q`; the extractor focuses on sections such as MD&A, current-report material events, and registration-statement strategy disclosure.
- Polygon.io is an optional exchange/profile source. It supports exchange profile and size metrics such as market cap, shares outstanding, and employee count. It can also provide a homepage URL that helps discover official investor-relations pages.
- Official company and investor-relations pages are optional enrichment sources. They supplement SEC filings with product, channel, geography, strategic priority, and company-description evidence that may be more current or more detailed than filing text.
- NewsAPI is an optional recent-news context source for the seller profile. Its configured lookback is short by default, and it is restricted to recognized business-media domains in the retrieval rules.
- OpenRouter/LLM extraction is used to turn the collected source text into a structured `TargetProfile`. It is not treated as an independent factual source. The extractor asks the LLM to return field support snippets, then maps those snippets back to source documents and labels fields as `verified_fact`, `derived_keyword`, or `llm_inference`.
- The builder stores source documents, evidence, feature-level labels, and the final profile in cache. The profile cache is keyed by a source fingerprint and extractor version so repeated runs can reuse stable results.

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
	- Method: use the `buyer_recall_strategic_public_companies` use case and EDGAR public-company metadata. The retriever performs light filtering only: it excludes the target itself, unnamed candidates, and candidates without same-SIC evidence.
	- Data source: EDGAR is the configured source. This is appropriate because SIC is part of the official public-company disclosure system and is more reliable than broad web classification for US-listed companies.

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
	- Data sources: LLM web search is the primary source for sponsor activity. The PE seed universe in `backend/config/pe_seed_universe.yaml` is only an identity filter; a candidate still needs recent relevant deal evidence before it is emitted. Financial Modeling Prep is the secondary configured source for M&A records that requires further tunning.
	- Parameters: the configured lookback is 5 years. The retriever also has caps on documents, queries, page size, candidates, scanned seed firms, and web-search batch size. Eligible sector matches are `same` and `adjacent`.

# Unified Retrieval Rule Configuration
1. Principles
	- Because the system retrieves from multiple data sources, and each source has its own availability, cost, cache behavior, and reliability profile, source configuration should live in one unified place.

2. Configuration method
`backend/config/retrieval_rules.yaml` is organized into three cooperating layers.

- `providers`
	- Defines provider availability and provider-specific retrieval knobs.
	- Examples include `enabled`, `required`, API key environment variables, cache TTLs, NewsAPI domains and lookback, Google News RSS locale, EDGAR markdown parsing settings, and LLM web-search/fetch limits.
	- Required providers, such as EDGAR, are part of the baseline pipeline. Optional providers can be disabled automatically when their API key or adapter is unavailable.

- `evidence_profiles`
	- Defines the evidence-strength policy for each provider and retrieval dimension.
	- For example, EDGAR target identity and filing metadata are treated as strong evidence, NewsAPI transaction news is supportive evidence, and LLM web-search extraction is a weaker signal that usually requires verification.
	- This keeps evidence strength tied to both the provider and the use case instead of assigning one global trust level to a source.

- `stages`
	- Binds pipeline stages to use cases, and use cases to source IDs plus source dimensions.
	- `target_profile_builder` defines use cases such as target resolution, SEC filings, exchange profile, business description, company strategy, and news discovery.
	- `potential_buyer_recaller` defines use cases such as strategic public-company recall, strategic acquisition intent, transaction signals, and financial sponsor activity.
	- The nested `retrievers` section maps concrete retriever classes to use cases, source roles, source priority, and retrieval parameters such as lookback windows, candidate caps, query caps, eligible sector matches, transaction terms, and EDGAR form/item settings.

Together these layers let the code ask the `DataSourceStrategy` for the selected source and retriever policy, instead of hardcoding provider switches, source strength, or retrieval parameters inside each retriever.

# Potential Buyer Hard Filter
This section is still primarily a design-stage component. The intended role is to remove candidates that clearly fail minimum acquisition conditions after candidate expansion and deduplication.

1. Principles
	- Use hard conditions to filter out companies that are obviously unsuitable acquirers.
	- Preserve recall during raw retrieval, then apply these hard filters after candidate normalization and deduplication.
	- Record every exclusion or pending-verification decision with machine-readable reasons.

2. Designed filter dimensions
- Self-candidate exclusion: the target company must never appear as its own buyer.
- Relevance requirement: a candidate needs at least one relevance path, such as same industry, adjacent industry, same customer segment, same channel, same product category, same supply chain, relevant acquisition history, or PE portfolio/deal fit.
- Evidence requirement: every formal long-list candidate should have at least one strong public evidence item. In the design docs, A/B evidence can support formal inclusion, C-only evidence goes to pending verification, and D-only broad-web evidence is excluded.
- Strategic buyer capacity: where data is available, filter or downgrade buyers that are too small, have insufficient revenue, lack cash or financing capacity, or show severe financial distress. Example rules include buyer market cap at least 1.5x target market cap, buyer revenue at least 2x target revenue, or enough cash/debt capacity to support a transaction.
- Financial buyer specificity: a PE firm should not enter the formal long-list merely because it is large or famous. It needs evidence of sector focus, portfolio fit, recent relevant deal activity, investment criteria fit, or platform-company add-on logic.

# Potential Buyer Analysis Filter
This section is also mainly design-stage methodology. It sits after raw candidate recall, normalization, deduplication, and hard filtering.

1. Principles
	- Build a multi-source profile for each retained buyer, then compare it against the seller profile from the perspective of acquisition likelihood.
	- Use a hybrid scoring method that combines rule-based signals, source evidence, and LLM-assisted classification or explanation.
	- Keep scoring rules in a unified configuration file so weights and thresholds can be tuned without changing retriever code.
	- Preserve source paths, citations, fit reasons, risk flags, and score components so the final buyer universe remains explainable.
	- Use LLMs for extraction, classification, and explanation; do not allow LLM-only reasoning to become the sole factual basis for candidate inclusion.

2. Designed scoring approach
- For strategic buyers, the planned long-list score uses:
	- Strategic fit: 30%
	- M&A history: 25%
	- Financial capacity: 20%
	- Geographic fit: 10%
	- Evidence strength: 10%
	- Data confidence: 5%
- For financial buyers, the planned long-list score uses:
	- Sector focus: 30%
	- Portfolio fit: 25%
	- Deal activity: 20%
	- Size fit: 15%
	- Evidence strength: 10%

3. Implementation technology choice
	- The intended approach is similar to a RAG reranking pipeline: retrieve a broad candidate pool first, then rerank using a combination of term-frequency/BM25-style matching, profile embedding similarity, structured rule scores, and evidence-aware LLM classification.
	- Deterministic methods should be used first for stable signals such as SIC match, source strength, deal count, geography, and size fit.
	- Embeddings and LLM judgments should be used for softer semantic signals such as business-description similarity, product/channel adjacency, platform add-on rationale, and strategic intent.
	- The final analysis filter should output not only a score, but also `why_included`, `risk_flags`, `source_paths`, `evidence_count`, and citations.
