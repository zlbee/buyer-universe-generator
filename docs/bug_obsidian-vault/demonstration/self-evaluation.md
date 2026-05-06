# Self-Evaluation

## Strengths

1. I consistently considered engineering quality during implementation. The system separates the data extraction layer, LLM capability layer, rule configuration layer, and pipeline stages. This structure improves future extensibility. In particular, the independent rule configuration layer makes the system more explainable and easier to tune.
2. I designed for caching, intermediate-result persistence, and auditability. This is important because both raw data-source calls and LLM calls can be slow and expensive. The stored records should also help future debugging, prompt tuning, retrieval tuning, and evidence review.
3. I built a reasonably complete seller profile from multiple data sources. Although the profile still has room for improvement, it already contains enough dimensions to serve as an anchor for downstream buyer analysis.
4. I gave significant attention to the `Potential Buyer Recaller`. The current implementation uses multiple recall paths and multiple data sources to retrieve possible strategic and financial buyers. It is not perfect yet, but it establishes an extensible framework that can support additional retrievers later.

## Weaknesses and Improvement Directions

1. My understanding of the M&A domain is still basic. It took a long time to arrive at the methodology, which affected implementation progress. Some detailed methodology decisions may also be incomplete or wrong.
  Improvement direction: learn more M&A domain knowledge and, if possible, ask practitioners or domain experts for feedback on buyer-screening logic, acquisition rationale, financial capacity assumptions, and long-list quality.
2. The seller-profile `keywords` field is currently extracted indirectly from raw source evidence with LLM assistance. This may introduce noise, and some keywords may not be highly summarized or discriminative word phrases.
  Improvement direction: improve keyword extraction with more deterministic NLP and retrieval techniques, such as named entity recognition, BM25 or term-frequency scoring, pretrained embeddings with clustering, and keyphrase extraction models. Better keywords should improve downstream retrieval and analysis accuracy.
3. Company IR page retrieval is not yet strong enough. The current approach tries to locate an IR page and then uses a more traditional crawling flow. During implementation, I found that LLM web search may be a better way to handle crawler-like discovery tasks, especially when site structures vary widely.
  Improvement direction: use LLM web search as a controlled source-discovery mechanism, then verify discovered pages by fetching cited pages, extracting snippets, and tying claims back to public evidence.
4. The `Potential Buyer Recaller` has multiple recall paths, but many planned retrievers are still incomplete or implemented as shells. Examples include `BusinessSimilarityRetriever`, `ProductCustomerChannelRetriever`, `PeerCompanyRetriever`, and `SupplyChainRetriever`. Some implemented retrievers also clearly need parameter tuning.
  Improvement direction: continue expanding the recaller and tune each retrieval path with real examples. The main goal should be improving recall while preserving evidence quality and traceability.

## Other Optimization Space

- **Tune retrieval parameters**. Retrieval parameters and retrieval methods for each data source need tuning to improve candidate-set recall and accuracy. Current recall still has room for improvement.
- **Tune LLM prompts**. LLM prompts throughout the system need tuning. Additional attention should be put on LLM with web search to balance the result quality and budget.
- **Preliminary filter & aggregation**. Add a real normalization and deduplication layer across CIK, ticker, domain, legal name, aliases, PE firm name, and platform-company name. Preliminary scoring could be performed based on hit count and dimensionality.
- Implement the hard-filter and pre-scoring stages as configuration-driven modules, with explainable exclusion reasons and score components.
- **Enhance Baseline Databases**. Improve core assets like the PE seed database and sector taxonomy. Expanding their data coverage will facilitate future analysis. Key upgrades include adding official portfolio links and investment mandates to the PE database, and populating the sector taxonomy with more granular industry keywords.
- **Improve pipeline availability**. The current system already has some graceful-degradation measures, but coverage is not yet 100%; one failed retrieval path should not cause the entire pipeline to fail.

## Overall Reflection

The current project is a solid framework/fundation rather than a finished buyer-universe engine. Its strongest parts are the engineering boundaries, data-source configuration, caching and audit design, seller-profile construction, and extensible first-pass buyer recall. Its main weaknesses are domain maturity, retrieval quality, incomplete downstream filtering, and incomplete planned retrievers.

The next development stage should focus more on making the candidate pipeline trustworthy end to end: better retrieval parameters, stronger evidence verification, real normalization and deduplication, configurable hard filters, and transparent score components.