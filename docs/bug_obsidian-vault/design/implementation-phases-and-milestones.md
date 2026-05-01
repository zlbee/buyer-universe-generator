# Implementation Phases and Milestones

## Purpose

This document breaks the Buyer Universe Generator long-list architecture into concrete implementation phases. The goal is to build a runnable MVP that accepts a US-listed target ticker or company name, extracts a traceable target profile, retrieves strategic and financial buyer candidates, and exports a defensible long-list with citations.

The implementation focus is limited to:

- `Target Feature Extractor`
- `Buyer Candidate Retriever`
- Normalization, hard filtering, pre-scoring, and long-list export

`Buyer Candidate Analyzer` and `Buyer Universe Generator` remain integration stubs with `TBD` internals until the long-list foundation is stable.

## Phase 0: Project Foundation

### Objective

Create a clean, runnable project skeleton with shared contracts, configuration, persistence, and development workflow.

### Implementation Scope

- Create backend package structure for `api`, `domain`, `pipelines`, `sources`, `retrievers`, `repositories`, `llm`, and `exporters`.
- Add Python 3.12 dependency management and application entrypoints.
- Add configuration with `pydantic-settings` for API keys, source toggles, cache paths, retrieval limits, and scoring thresholds.
- Add SQLite and SQLAlchemy 2.0 setup.
- Add base Pydantic models for `Evidence`, `TargetProfile`, `CandidateHit`, `LongListCandidate`, and `PipelineRun`.
- Add README instructions for local setup and basic execution.
- Add pytest setup with a small smoke test.

### Acceptance Criteria

- A fresh checkout can install dependencies and run the backend locally.
- `pytest` runs successfully with at least one smoke test.
- The application can load configuration from environment variables and defaults.
- SQLite initializes without manual steps.
- Domain models serialize to JSON and validate required fields.
- README includes exact setup and run commands.

## Phase 1: Target Resolution and Source Ingestion

### Objective

Resolve a user-provided ticker or company name into a canonical US-listed target identity and collect the raw public source materials needed for extraction.

### Implementation Scope

- Implement target resolver for ticker and exact company name lookup.
- Resolve at minimum: legal name, ticker, CIK, exchange, and SEC entity metadata.
- Implement SEC EDGAR client for company submissions and recent filings.
- Fetch latest available 10-K and relevant 10-Q/8-K metadata.
- Add source document cache with URL, source type, retrieval timestamp, and raw text or parsed metadata.
- Add controlled error handling for ambiguous names, missing tickers, missing filings, and SEC/network failures.

### Acceptance Criteria

- Given a valid US-listed ticker, the system resolves CIK, company name, ticker, and exchange.
- Given an exact company name for the same target, the system resolves to the same canonical identity.
- The system fetches and caches SEC submissions and latest 10-K metadata.
- Re-running the same target uses cached source metadata when allowed by configuration.
- Invalid or ambiguous inputs return clear structured errors.
- Tests cover valid ticker, valid company name, missing target, and cached re-run behavior.

## Phase 2: Target Feature Extractor

### Objective

Generate a minimum but decision-driving `TargetProfile` that anchors all downstream retrieval paths.

### Implementation Scope

- Extract identity, SIC, filing references, business description, products, customer segments, channels, geographies, and size metrics where available.
- Use SEC filings as the primary source for identity, SIC, business description, and financial facts.
- Add company or IR page enrichment as a secondary source when a reliable official page is discoverable.
- Add LLM-assisted structured extraction for products, customer segments, channels, adjacent categories, and retrieval keywords.
- Mark every feature as either `verified_fact`, `derived_keyword`, or `llm_inference`.
- Attach evidence to decision-driving fields using the shared `Evidence` model.
- Emit a retrieval-ready profile with keywords and adjacent categories.

### Acceptance Criteria

- For a selected demo target, the extractor returns all required `TargetProfile` fields.
- Every decision-driving field has at least one source reference or is explicitly marked as inference.
- LLM-generated fields are never marked as verified facts unless supported by source evidence.
- The extractor can run without company-page enrichment if SEC data is available.
- Invalid LLM JSON or missing optional fields do not crash the pipeline.
- Tests validate schema completeness, evidence attachment, and fact-versus-inference labeling.

## Phase 3: Provenance, Caching, and Evidence Layer

### Objective

Make all retrieved facts and candidates traceable, repeatable, and auditable.

### Implementation Scope

- Persist pipeline runs, source documents, extracted features, candidate hits, evidence records, and export artifacts.
- Implement source strength levels:
  - `A`: SEC filings, official company pages, official IR pages, PE official portfolio pages
  - `B`: reliable news, press releases, recognized business media
  - `C`: Wikidata, exchange data, company profile databases
  - `D`: broad open web discovery
- Add repository methods for creating and reading run state.
- Add cache expiration settings per source type.
- Add provenance fields to every candidate and evidence item.

### Acceptance Criteria

- A full target extraction run can be inspected from SQLite after completion.
- Every evidence record includes claim, source type, source strength, URL or filing reference, and retrieval timestamp.
- Cache behavior is configurable and visible in logs or run metadata.
- Source strength is assigned consistently by source adapter.
- Tests cover evidence persistence, cache hit/miss behavior, and source strength assignment.

## Phase 4: Strategic Buyer Candidate Retrieval

### Objective

Retrieve high-recall strategic buyer candidates through multiple independent retrieval paths.

### Implementation Scope

- Implement `SameSicRetriever` using SEC or public company metadata.
- Implement `BusinessSimilarityRetriever` using BM25/TF-IDF over available public company business descriptions.
- Implement `ProductCustomerChannelRetriever` using target keywords and official/filing text.
- Implement `MAHistoryRetriever` using SEC 8-K, 10-K acquisition references, company announcements, and reliable news where available.
- Implement `PeerCompanyRetriever` with public peer hints from filings, company materials, Wikidata/Wikipedia, or structured public sources.
- Implement `SupplyChainRetriever` as a keyword and category-driven retriever for upstream, downstream, channel, platform, and vertical integration candidates.
- Each retriever must emit `CandidateHit` records instead of final long-list candidates.

### Acceptance Criteria

- Each strategic retriever can be run independently from the same `TargetProfile`.
- Every emitted candidate hit includes `retriever_name`, `source_path`, `fit_reason`, confidence, and at least one evidence item or a pending-verification status.
- No retriever can promote a candidate into the final long-list by itself without passing normalization and hard filters.
- `MAHistoryRetriever` marks deal date, acquired target, sector match, and source where available.
- Tests use fixtures to verify each retriever emits deterministic `CandidateHit` objects.
- At least three strategic retrieval paths produce non-empty results for the selected demo target.

## Phase 5: Financial Buyer Candidate Retrieval

### Objective

Retrieve financial buyer candidates from public PE portfolio, investment criteria, and deal activity signals.

### Implementation Scope

- Define an initial configurable PE seed universe for MVP.
- Implement `PEPortfolioRetriever` using PE official portfolio pages and portfolio company pages.
- Implement `PEDealActivityRetriever` using PE official announcements, public news, and company press releases.
- Extract platform-company add-on logic where a PE firm owns a relevant platform company.
- Extract investment criteria where publicly available: sector, geography, revenue, EBITDA, enterprise value, control/minority preference.
- Emit financial `CandidateHit` records with firm name, optional platform company, source path, evidence, and fit reason.

### Acceptance Criteria

- The PE seed universe is configuration-driven and documented.
- A financial candidate cannot enter the formal long-list only because the PE firm is large or famous.
- Each formal financial candidate has evidence for sector focus, portfolio fit, recent deal activity, or investment criteria.
- Platform-company candidates preserve both PE firm and platform company names.
- Tests cover PE portfolio fit, recent deal activity fit, missing portfolio evidence, and seed-list configuration.
- At least one financial retrieval path produces non-empty results for the selected demo target.

## Phase 6: Candidate Normalization, Deduplication, and Hard Filters

### Objective

Merge noisy multi-source candidate hits into canonical candidates and remove candidates that do not meet minimum long-list standards.

### Implementation Scope

- Normalize strategic buyers by CIK, ticker, company domain, legal name, and aliases.
- Normalize financial buyers by firm name, website domain, aliases, and platform company.
- Merge duplicate hits while preserving source paths, evidence, fit reasons, and hit count.
- Implement hard filters:
  - Exclude the target itself.
  - Require at least one relevance path.
  - Require at least one `A` or `B` evidence item for formal long-list inclusion.
  - Put `C`-only candidates into pending verification.
  - Exclude `D`-only candidates.
  - Apply buyer capacity or size filters where data is available.
  - Apply geography and public-data availability filters.
- Record filter decisions and risk flags.

### Acceptance Criteria

- The same buyer from multiple retrievers appears once in the normalized output.
- Source paths and hit counts are preserved after deduplication.
- Formal long-list candidates have at least one `A` or `B` evidence item.
- Excluded and pending-verification candidates include machine-readable reasons.
- The target company is never included as its own buyer.
- Tests cover duplicate merging, alias handling, evidence-strength filtering, target-self exclusion, and pending-verification routing.

## Phase 7: Long-List Pre-Scoring and Export

### Objective

Produce a sorted, structured, and human-readable long-list suitable for downstream analysis and review.

### Implementation Scope

- Implement configurable scoring weights.
- Strategic buyer default score components:
  - Strategic fit: 30%
  - M&A history: 25%
  - Financial capacity: 20%
  - Geographic fit: 10%
  - Evidence strength: 10%
  - Data confidence: 5%
- Financial buyer default score components:
  - Sector focus: 30%
  - Portfolio fit: 25%
  - Deal activity: 20%
  - Size fit: 15%
  - Evidence strength: 10%
- Export JSON and CSV outputs.
- Export a banker-readable Markdown or HTML summary.
- Include score components, source paths, fit reasons, risk flags, and citations in exports.

### Acceptance Criteria

- Scoring is deterministic for a fixed run and fixed configuration.
- Each exported candidate includes canonical name, buyer type, score, source paths, fit reasons, evidence, and risk flags.
- JSON export validates against the `LongListCandidate` schema.
- CSV export can be opened with all critical fields present.
- Human-readable export includes clickable citation URLs or filing references.
- Tests verify score calculation, export schemas, and stable ordering.

## Phase 8: API, CLI, and Optional Frontend

### Objective

Make the pipeline runnable end-to-end by reviewers with minimal setup.

### Implementation Scope

- Add FastAPI endpoints:
  - Create pipeline run
  - Get run status
  - Get target profile
  - Get long-list results
  - Download exports
- Add CLI command for local execution by ticker or company name.
- Add background task orchestration for long-running runs.
- Add basic logs for source retrieval, extraction, retriever progress, filtering, and export generation.
- Optional frontend: simple React UI for entering target, viewing run status, and inspecting long-list outputs.

### Acceptance Criteria

- A reviewer can run the full pipeline from CLI using a ticker.
- A reviewer can run the full pipeline from API using a ticker or company name.
- Run status is visible while the pipeline is executing.
- API responses use the same domain schemas as the backend pipeline.
- Export files can be retrieved after run completion.
- End-to-end smoke test runs from input target to exported long-list.

## Phase 9: Evaluation, Documentation, and Demo Readiness

### Objective

Prepare the take-home submission with clear methodology, honest self-evaluation, and reproducible demo behavior.

### Implementation Scope

- Select and document one US-listed mid-market demo target.
- Add methodology document describing sources used, scoring, rejected sources, reliability, and LLM usage.
- Add self-evaluation document covering false positives, blind spots, hallucination risk, missing data, and future improvement metrics.
- Add reviewer-facing README section for running the demo target and a live ticker.
- Add fixture-based regression test for the selected demo target where practical.
- Add sample JSON/CSV/human-readable outputs.
- Add known limitations and next-step roadmap.

### Acceptance Criteria

- README explains setup, environment variables, run commands, outputs, and troubleshooting.
- Methodology clearly distinguishes verified facts from LLM-generated inferences.
- Self-evaluation names concrete failure modes and how future versions would be measured.
- Sample outputs include citations for every formal long-list candidate.
- The demo target can be reproduced from a clean checkout.
- The system can accept a different live ticker without target-specific code changes.

## Cross-Phase Definition of Done

A phase is considered complete only when:

- The implementation is covered by focused tests or a documented smoke test.
- New public interfaces are represented in Pydantic models or API contracts.
- Non-obvious logic has clear English comments.
- Configuration values are not hardcoded unless they are stable constants.
- Evidence and provenance are preserved for facts that affect candidate inclusion.
- Failure modes return structured errors or recorded warnings instead of silent failures.

## Milestone Summary


| Milestone                    | Phases Included | Deliverable                                                          |
| ---------------------------- | --------------- | -------------------------------------------------------------------- |
| M1: Runnable Foundation      | Phase 0         | Project skeleton, config, database, base models, test harness        |
| M2: Target Profile           | Phases 1-2      | Resolved target identity and evidence-backed `TargetProfile`         |
| M3: Traceable Retrieval Core | Phases 3-5      | Strategic and financial `CandidateHit` generation with provenance    |
| M4: Formal Long-List         | Phases 6-7      | Deduped, filtered, scored, exportable long-list                      |
| M5: End-to-End MVP           | Phase 8         | CLI/API runnable pipeline from input target to exports               |
| M6: Submission Ready         | Phase 9         | README, methodology, self-evaluation, sample outputs, demo readiness |


## Explicit TBD Boundaries

- `Buyer Candidate Analyzer`: deeper evidence collection, final ranking, thesis generation, and banker-grade scoring are not implemented in the long-list MVP.
- `Buyer Universe Generator`: final 30-50 buyer selection, report packaging, banker review workflow, and presentation-ready output are not implemented in the long-list MVP.
- International targets, paid databases, and production-grade job queues are out of scope for the first implementation pass.

