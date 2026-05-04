import { useEffect, useMemo, useState } from "react";

type HealthResponse = {
  status: string;
  app: string;
  environment: string;
  version: string;
};

type HealthState =
  | { status: "checking" }
  | { status: "online"; data: HealthResponse }
  | { status: "offline"; message: string };

type ResolvedTarget = {
  canonical_name: string;
  ticker: string;
  cik: string;
  exchange: string | null;
  sic: string | null;
  resolution_confidence: number;
  matched_input: string;
  source_provenance: string[];
};

type Evidence = {
  claim: string;
  source_type: string;
  source_dimension: string | null;
  source_strength: string;
  url: string | null;
  filing_accession: string | null;
  retrieved_at: string;
  quote_or_snippet: string | null;
  verified_fact: boolean;
};

type SourceDocument = {
  source_id: string;
  source_dimension: string | null;
  source_type: string;
  source_strength: string;
  target_cik: string | null;
  target_ticker: string | null;
  url: string | null;
  filing_accession: string | null;
  raw_text: string | null;
  metadata: Record<string, unknown>;
  retrieved_at: string;
  expires_at: string | null;
};

type TargetProfile = {
  target_id: string;
  name: string;
  ticker: string;
  cik: string;
  exchange: string | null;
  sic: string | null;
  business_summary: string | null;
  company_strategy: string | null;
  products: string[];
  customer_segments: string[];
  channels: string[];
  geographies: string[];
  size_metrics: Record<string, unknown>;
  keywords: string[];
  keyword_groups: Record<string, string[]>;
  adjacent_categories: string[];
  feature_labels: Record<string, "verified_fact" | "derived_keyword" | "llm_inference">;
  feature_evidence: Record<string, Evidence[]>;
  evidence: Evidence[];
};

type TargetProfileExtractionResult = {
  target_profile: TargetProfile;
  source_documents: SourceDocument[];
  warnings: string[];
  extraction_metadata: Record<string, unknown>;
};

type CompanyFinancialMetrics = {
  canonical_name: string;
  ticker: string;
  cik: string;
  exchange: string | null;
  sic: string | null;
  market_cap_usd: number | null;
  revenue_usd: number | null;
  cash_and_equivalents_usd: number | null;
  revenue_period_end: string | null;
  revenue_period_type: string | null;
  revenue_fiscal_year: number | null;
  cash_period_end: string | null;
  cash_fiscal_year: number | null;
  currency: string;
  metric_sources: Record<string, string>;
  evidence: Evidence[];
};

type AcquirerCapacityRuleResult = {
  rule_id: string;
  status: "pass" | "fail" | "unknown";
  observed_value_usd: number | null;
  threshold_usd: number | null;
  ratio: number | null;
  missing_fields: string[];
  warning: string | null;
};

type AcquirerCapabilityCandidate = {
  canonical_name: string;
  ticker: string;
  cik: string;
  exchange: string | null;
  sic: string | null;
  metrics: CompanyFinancialMetrics;
  rule_results: AcquirerCapacityRuleResult[];
  passed_rules: string[];
  risk_flags: string[];
  evidence: Evidence[];
};

type AcquirerCapabilityUniverseResult = {
  target: ResolvedTarget;
  target_metrics: CompanyFinancialMetrics;
  thresholds: Record<string, number | null>;
  candidates: AcquirerCapabilityCandidate[];
  excluded_counts: Record<string, number>;
  source_coverage: Record<string, unknown>;
  warnings: string[];
  generated_at: string;
};

type ProfileState =
  | { status: "idle" }
  | { status: "loading"; query: string }
  | { status: "success"; query: string; data: TargetProfileExtractionResult }
  | { status: "error"; query: string; message: string; errorCode?: string; candidates?: ResolvedTarget[] };

type UniverseState =
  | { status: "idle" }
  | { status: "loading"; query: string }
  | { status: "success"; query: string; data: AcquirerCapabilityUniverseResult }
  | { status: "error"; query: string; message: string; errorCode?: string; candidates?: ResolvedTarget[] };

type ApiError = Error & { candidates?: ResolvedTarget[]; errorCode?: string };

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

function App() {
  const [targetInput, setTargetInput] = useState("ELF");
  const [health, setHealth] = useState<HealthState>({ status: "checking" });
  const [profileState, setProfileState] = useState<ProfileState>({ status: "idle" });
  const [universeState, setUniverseState] = useState<UniverseState>({ status: "idle" });

  const trimmedTarget = useMemo(() => targetInput.trim(), [targetInput]);

  useEffect(() => {
    const controller = new AbortController();

    async function checkHealth() {
      try {
        const response = await fetch(`${API_BASE_URL}/health`, {
          signal: controller.signal
        });

        if (!response.ok) {
          throw new Error(`Backend returned HTTP ${response.status}`);
        }

        const data = (await response.json()) as HealthResponse;
        setHealth({ status: "online", data });
      } catch (error) {
        if (controller.signal.aborted) {
          return;
        }

        const message = error instanceof Error ? error.message : "Unknown backend health error";
        setHealth({ status: "offline", message });
      }
    }

    void checkHealth();

    return () => controller.abort();
  }, []);

  async function handleTargetProfileBuild() {
    if (!trimmedTarget || profileState.status === "loading") {
      return;
    }

    setProfileState({ status: "loading", query: trimmedTarget });

    try {
      const response = await fetch(`${API_BASE_URL}/targets/profile?query=${encodeURIComponent(trimmedTarget)}`);

      if (!response.ok) {
        throw await readApiError(response);
      }

      const data = (await response.json()) as TargetProfileExtractionResult;
      setProfileState({ status: "success", query: trimmedTarget, data });
    } catch (error) {
      setProfileState(errorStateFromApiError(error, trimmedTarget, "Unknown TargetProfile error"));
    }
  }

  async function handleAcquirerUniverseBuild() {
    if (!trimmedTarget || universeState.status === "loading") {
      return;
    }

    setUniverseState({ status: "loading", query: trimmedTarget });

    try {
      const response = await fetch(
        `${API_BASE_URL}/buyers/acquirer-capable-universe?query=${encodeURIComponent(trimmedTarget)}`
      );

      if (!response.ok) {
        throw await readApiError(response);
      }

      const data = (await response.json()) as AcquirerCapabilityUniverseResult;
      setUniverseState({ status: "success", query: trimmedTarget, data });
    } catch (error) {
      setUniverseState(errorStateFromApiError(error, trimmedTarget, "Unknown Acquirer-Capable Universe error"));
    }
  }

  const profileCardMetrics = profileSummary(profileState);
  const universeCardMetrics = universeSummary(universeState);

  return (
    <main className="app-shell">
      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">Long-list MVP</p>
            <h1>Buyer Universe Generator</h1>
          </div>
          <div className="topbar-actions">
            <a className="debug-entry-link" href="#target-profile-stage">
              Profile
            </a>
            <a className="debug-entry-link" href="#acquirer-universe-stage">
              Universe
            </a>
            <BackendStatus health={health} />
          </div>
        </header>

        <section className="control-panel" aria-labelledby="target-form-title">
          <div className="panel-copy">
            <h2 id="target-form-title">Target input</h2>
            <p>Shared target for the active retrieval stages.</p>
          </div>

          <div className="target-form">
            <label htmlFor="target-input">Target ticker or company name</label>
            <div className="input-row">
              <input
                id="target-input"
                name="target"
                value={targetInput}
                onChange={(event) => setTargetInput(event.target.value)}
                placeholder="e.g. ELF or e.l.f. Beauty"
                autoComplete="off"
              />
            </div>
          </div>
        </section>

        <section className="stage-grid" aria-label="Pipeline stages">
          <PageCard
            eyebrow="Phase 2"
            title="TargetProfile Extractor"
            status={profileState.status}
            actionLabel="Build Profile"
            loadingLabel="Building"
            disabled={!trimmedTarget}
            onAction={handleTargetProfileBuild}
            href="#target-profile-stage"
            metrics={profileCardMetrics}
          />
          <PageCard
            eyebrow="Phase 3"
            title="Acquirer-Capable Universe Builder"
            status={universeState.status}
            actionLabel="Build Universe"
            loadingLabel="Screening"
            disabled={!trimmedTarget}
            onAction={handleAcquirerUniverseBuild}
            href="#acquirer-universe-stage"
            metrics={universeCardMetrics}
          />
        </section>

        <TargetProfileStagePanel state={profileState} />
        <AcquirerUniverseStagePanel state={universeState} />
      </section>
    </main>
  );
}

function BackendStatus({ health }: { health: HealthState }) {
  if (health.status === "online") {
    return (
      <div className="backend-status backend-status--online">
        <span>Backend online</span>
        <small>
          {health.data.environment} · v{health.data.version}
        </small>
      </div>
    );
  }

  if (health.status === "offline") {
    return (
      <div className="backend-status backend-status--offline">
        <span>Backend offline</span>
        <small>{health.message}</small>
      </div>
    );
  }

  return (
    <div className="backend-status">
      <span>Checking backend</span>
      <small>{API_BASE_URL}</small>
    </div>
  );
}

function PageCard({
  eyebrow,
  title,
  status,
  actionLabel,
  loadingLabel,
  disabled,
  onAction,
  href,
  metrics
}: {
  eyebrow: string;
  title: string;
  status: ProfileState["status"] | UniverseState["status"];
  actionLabel: string;
  loadingLabel: string;
  disabled: boolean;
  onAction: () => void;
  href: string;
  metrics: Array<{ label: string; value: string }>;
}) {
  const isLoading = status === "loading";

  return (
    <article className="page-card">
      <div className="page-card__header">
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h2>{title}</h2>
        </div>
        <span className={`debug-state debug-state--${status}`}>{status}</span>
      </div>

      <dl className="page-card__metrics">
        {metrics.map((metric) => (
          <div key={metric.label}>
            <dt>{metric.label}</dt>
            <dd>{metric.value}</dd>
          </div>
        ))}
      </dl>

      <div className="page-card__actions">
        <button type="button" onClick={onAction} disabled={disabled || isLoading}>
          {isLoading ? loadingLabel : actionLabel}
        </button>
        <a href={href}>Open result</a>
      </div>
    </article>
  );
}

function TargetProfileStagePanel({ state }: { state: ProfileState }) {
  return (
    <section className="debug-panel" id="target-profile-stage" aria-labelledby="target-profile-stage-title">
      <div className="debug-panel__header">
        <div>
          <p className="eyebrow">Stage output</p>
          <h2 id="target-profile-stage-title">TargetProfile Extractor</h2>
        </div>
        <span className={`debug-state debug-state--${state.status}`}>{state.status}</span>
      </div>

      {state.status === "idle" && <div className="debug-empty">No TargetProfile run yet.</div>}
      {state.status === "loading" && <div className="debug-empty">Building TargetProfile for {state.query}.</div>}
      {state.status === "error" && (
        <DebugError message={state.message} errorCode={state.errorCode} candidates={state.candidates} />
      )}
      {state.status === "success" && <TargetProfileResult state={state} />}
    </section>
  );
}

function AcquirerUniverseStagePanel({ state }: { state: UniverseState }) {
  return (
    <section className="debug-panel" id="acquirer-universe-stage" aria-labelledby="acquirer-universe-stage-title">
      <div className="debug-panel__header">
        <div>
          <p className="eyebrow">Stage output</p>
          <h2 id="acquirer-universe-stage-title">Acquirer-Capable Universe Builder</h2>
        </div>
        <span className={`debug-state debug-state--${state.status}`}>{state.status}</span>
      </div>

      {state.status === "idle" && <div className="debug-empty">No Acquirer-Capable Universe run yet.</div>}
      {state.status === "loading" && <div className="debug-empty">Screening public-company buyers for {state.query}.</div>}
      {state.status === "error" && (
        <DebugError message={state.message} errorCode={state.errorCode} candidates={state.candidates} />
      )}
      {state.status === "success" && <AcquirerUniverseResult state={state} />}
    </section>
  );
}

function DebugError({
  message,
  errorCode,
  candidates
}: {
  message: string;
  errorCode?: string;
  candidates?: ResolvedTarget[];
}) {
  return (
    <div className="debug-error">
      <strong>{message}</strong>
      {errorCode && <p>{errorCode}</p>}
      {candidates && candidates.length > 0 && (
        <div className="debug-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Ticker</th>
                <th>CIK</th>
                <th>Exchange</th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((candidate) => (
                <tr key={`${candidate.cik}-${candidate.ticker}`}>
                  <td>{candidate.canonical_name}</td>
                  <td>{candidate.ticker}</td>
                  <td>{candidate.cik}</td>
                  <td>{candidate.exchange ?? "N/A"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function TargetProfileResult({ state }: { state: Extract<ProfileState, { status: "success" }> }) {
  const { data } = state;
  const profile = data.target_profile;

  return (
    <div className="debug-result">
      <WarningList warnings={data.warnings} />

      <div className="debug-section">
        <h3>Resolved Target</h3>
        <dl className="debug-metrics">
          <DebugMetric label="Name" value={profile.name} />
          <DebugMetric label="Ticker" value={profile.ticker} />
          <DebugMetric label="CIK" value={profile.cik} />
          <DebugMetric label="Exchange" value={profile.exchange ?? "N/A"} />
          <DebugMetric label="SIC" value={profile.sic ?? "N/A"} />
          <DebugMetric label="Evidence" value={formatCount(profile.evidence.length)} />
        </dl>
      </div>

      <div className="debug-section">
        <h3>TargetProfile</h3>
        <div className="debug-json-grid">
          <pre>{JSON.stringify(profile, null, 2)}</pre>
          <div className="debug-field-list">
            <strong>Feature labels</strong>
            {Object.entries(profile.feature_labels).length > 0 ? (
              Object.entries(profile.feature_labels).map(([field, label]) => (
                <span key={field}>
                  {field}: {label}
                </span>
              ))
            ) : (
              <span>N/A</span>
            )}
          </div>
        </div>
      </div>

      <EvidenceTable evidenceByField={profile.feature_evidence} />
      <SourceDocumentTable documents={data.source_documents} />

      <details className="debug-raw">
        <summary>Extraction metadata</summary>
        <pre>{JSON.stringify(data.extraction_metadata, null, 2)}</pre>
      </details>

      <details className="debug-raw">
        <summary>Raw API response</summary>
        <pre>{JSON.stringify(data, null, 2)}</pre>
      </details>
    </div>
  );
}

function AcquirerUniverseResult({ state }: { state: Extract<UniverseState, { status: "success" }> }) {
  const { data } = state;

  return (
    <div className="debug-result">
      <WarningList warnings={data.warnings} />

      <div className="debug-section">
        <h3>Target Capacity Baseline</h3>
        <dl className="debug-metrics">
          <DebugMetric label="Ticker" value={data.target.ticker} />
          <DebugMetric label="Market cap" value={formatUsd(data.target_metrics.market_cap_usd)} />
          <DebugMetric label="Revenue" value={formatUsd(data.target_metrics.revenue_usd)} />
          <DebugMetric label="Cash" value={formatUsd(data.target_metrics.cash_and_equivalents_usd)} />
          <DebugMetric label="Revenue period" value={data.target_metrics.revenue_period_end ?? "N/A"} />
          <DebugMetric label="Candidates" value={formatCount(data.candidates.length)} />
        </dl>
      </div>

      <div className="debug-section">
        <h3>Capacity Thresholds</h3>
        <dl className="debug-metrics">
          <DebugMetric label="Market cap min" value={formatUsd(data.thresholds.candidate_market_cap_min_usd)} />
          <DebugMetric label="Cash min" value={formatUsd(data.thresholds.candidate_cash_min_usd)} />
          <DebugMetric label="Revenue min" value={formatUsd(data.thresholds.candidate_revenue_min_usd)} />
          <DebugMetric label="Seed companies" value={formatUnknownCount(data.source_coverage.seed_companies)} />
          <DebugMetric label="Screened" value={formatUnknownCount(data.source_coverage.screened_companies)} />
          <DebugMetric label="Market cap coverage" value={formatUnknownCount(data.source_coverage.polygon_market_cap_available)} />
        </dl>
      </div>

      <div className="debug-section">
        <h3>Passing Candidates</h3>
        <div className="debug-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Rank</th>
                <th>Buyer</th>
                <th>Ticker</th>
                <th>Exchange</th>
                <th>Passed rules</th>
                <th>Market cap</th>
                <th>Revenue</th>
                <th>Cash</th>
                <th>Risk flags</th>
              </tr>
            </thead>
            <tbody>
              {data.candidates.map((candidate, index) => (
                <tr key={`${candidate.cik}-${candidate.ticker}`}>
                  <td>{index + 1}</td>
                  <td>{candidate.canonical_name}</td>
                  <td>{candidate.ticker}</td>
                  <td>{candidate.exchange ?? "N/A"}</td>
                  <td>{candidate.passed_rules.join(", ")}</td>
                  <td>{formatUsd(candidate.metrics.market_cap_usd)}</td>
                  <td>{formatUsd(candidate.metrics.revenue_usd)}</td>
                  <td>{formatUsd(candidate.metrics.cash_and_equivalents_usd)}</td>
                  <td>{candidate.risk_flags.length > 0 ? candidate.risk_flags.join(", ") : "N/A"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="debug-section">
        <h3>Exclusions</h3>
        <div className="debug-field-list debug-field-list--wide">
          {Object.entries(data.excluded_counts).length > 0 ? (
            Object.entries(data.excluded_counts).map(([reason, count]) => (
              <span key={reason}>
                {reason}: {formatCount(count)}
              </span>
            ))
          ) : (
            <span>N/A</span>
          )}
        </div>
      </div>

      <details className="debug-raw">
        <summary>Raw API response</summary>
        <pre>{JSON.stringify(data, null, 2)}</pre>
      </details>
    </div>
  );
}

function EvidenceTable({ evidenceByField }: { evidenceByField: Record<string, Evidence[]> }) {
  const rows = Object.entries(evidenceByField).flatMap(([field, evidenceItems]) =>
    evidenceItems.map((evidence, index) => ({ field, evidence, index }))
  );

  return (
    <div className="debug-section">
      <h3>Feature Evidence</h3>
      <div className="debug-table-wrap">
        <table>
          <thead>
            <tr>
              <th>Field</th>
              <th>Strength</th>
              <th>Dimension</th>
              <th>Claim</th>
              <th>Reference</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ field, evidence, index }) => (
              <tr key={`${field}-${index}-${evidence.filing_accession ?? evidence.url}`}>
                <td>{field}</td>
                <td>{evidence.source_strength}</td>
                <td>{evidence.source_dimension ?? "N/A"}</td>
                <td>{evidence.claim}</td>
                <td>
                  {evidence.url ? (
                    <a href={evidence.url} target="_blank" rel="noreferrer">
                      Open
                    </a>
                  ) : (
                    evidence.filing_accession ?? "N/A"
                  )}
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={5}>N/A</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SourceDocumentTable({ documents }: { documents: SourceDocument[] }) {
  return (
    <div className="debug-section">
      <h3>Source Documents</h3>
      <div className="debug-table-wrap">
        <table>
          <thead>
            <tr>
              <th>Source</th>
              <th>Dimension</th>
              <th>Type</th>
              <th>Strength</th>
              <th>Text</th>
              <th>Reference</th>
            </tr>
          </thead>
          <tbody>
            {documents.map((document, index) => (
              <tr key={`${document.source_id}-${document.filing_accession ?? document.url ?? index}`}>
                <td>{document.source_id}</td>
                <td>{document.source_dimension ?? "N/A"}</td>
                <td>{document.source_type}</td>
                <td>{document.source_strength}</td>
                <td>{document.raw_text ? `${document.raw_text.length} chars` : "metadata"}</td>
                <td>
                  {document.url ? (
                    <a href={document.url} target="_blank" rel="noreferrer">
                      Open
                    </a>
                  ) : (
                    document.filing_accession ?? "metadata"
                  )}
                </td>
              </tr>
            ))}
            {documents.length === 0 && (
              <tr>
                <td colSpan={6}>N/A</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function WarningList({ warnings }: { warnings: string[] }) {
  if (warnings.length === 0) {
    return null;
  }

  return (
    <div className="debug-warning-list" aria-label="Source warnings">
      {warnings.map((warning) => (
        <span key={warning}>{warning}</span>
      ))}
    </div>
  );
}

function DebugMetric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function profileSummary(state: ProfileState): Array<{ label: string; value: string }> {
  if (state.status === "success") {
    const profile = state.data.target_profile;
    return [
      { label: "Ticker", value: profile.ticker },
      { label: "Features", value: formatCount(Object.keys(profile.feature_labels).length) },
      { label: "Evidence", value: formatCount(profile.evidence.length) }
    ];
  }

  if (state.status === "error") {
    return [
      { label: "Status", value: "Error" },
      { label: "Code", value: state.errorCode ?? "N/A" },
      { label: "Candidates", value: formatCount(state.candidates?.length ?? 0) }
    ];
  }

  return [
    { label: "Input", value: state.status === "loading" ? state.query : "Ready" },
    { label: "Sources", value: "SEC / IR" },
    { label: "Output", value: "Profile" }
  ];
}

function universeSummary(state: UniverseState): Array<{ label: string; value: string }> {
  if (state.status === "success") {
    return [
      { label: "Target", value: state.data.target.ticker },
      { label: "Candidates", value: formatCount(state.data.candidates.length) },
      { label: "Screened", value: formatUnknownCount(state.data.source_coverage.screened_companies) }
    ];
  }

  if (state.status === "error") {
    return [
      { label: "Status", value: "Error" },
      { label: "Code", value: state.errorCode ?? "N/A" },
      { label: "Candidates", value: formatCount(state.candidates?.length ?? 0) }
    ];
  }

  return [
    { label: "Input", value: state.status === "loading" ? state.query : "Ready" },
    { label: "Rules", value: "3" },
    { label: "Output", value: "Universe" }
  ];
}

function formatUsd(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "N/A";
  }

  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: Math.abs(value) >= 1_000_000_000 ? "compact" : "standard",
    maximumFractionDigits: 0
  }).format(value);
}

function formatCount(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

function formatUnknownCount(value: unknown): string {
  return typeof value === "number" ? formatCount(value) : "N/A";
}

async function readApiError(response: Response): Promise<ApiError> {
  try {
    const payload = (await response.json()) as {
      detail?: string | { message?: string; error_code?: string; candidates?: ResolvedTarget[] };
    };
    const detail = payload.detail;
    if (typeof detail === "string") {
      return Object.assign(new Error(detail), {});
    }

    return Object.assign(new Error(detail?.message ?? `Backend returned HTTP ${response.status}`), {
      candidates: detail?.candidates,
      errorCode: detail?.error_code
    });
  } catch {
    return Object.assign(new Error(`Backend returned HTTP ${response.status}`), {});
  }
}

function errorStateFromApiError<T extends ProfileState | UniverseState>(
  error: unknown,
  query: string,
  fallbackMessage: string
): Extract<T, { status: "error" }> {
  if (error instanceof Error) {
    const apiError = error as ApiError;
    return {
      status: "error",
      query,
      message: apiError.message,
      errorCode: apiError.errorCode,
      candidates: apiError.candidates
    } as Extract<T, { status: "error" }>;
  }

  return { status: "error", query, message: fallbackMessage } as Extract<T, { status: "error" }>;
}

export default App;
