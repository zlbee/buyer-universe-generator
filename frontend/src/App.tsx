import { useEffect, useMemo, useState, type FormEvent } from "react";

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

type FilingMetadata = {
  form: string;
  filing_date: string | null;
  accession_number: string;
  period_of_report: string | null;
  url: string | null;
  source_type: string;
};

type SourceDocument = {
  source_id: string;
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

type TargetIngestionResult = {
  target: ResolvedTarget;
  filings: FilingMetadata[];
  source_documents: SourceDocument[];
  warnings: string[];
};

type TargetProfileDraft = {
  target_id: string;
  name: string;
  ticker: string;
  cik: string;
  exchange: string | null;
  sic: string | null;
  business_summary: string | null;
  products: string[];
  customer_segments: string[];
  channels: string[];
  geographies: string[];
  size_metrics: Record<string, string | number>;
  keywords: string[];
  adjacent_categories: string[];
  feature_labels: Record<string, "verified_fact" | "derived_keyword" | "llm_inference">;
  pending_fields: string[];
  evidence_source_count: number;
};

type TargetProfileDebugState =
  | { status: "idle" }
  | { status: "loading"; query: string }
  | { status: "success"; query: string; data: TargetIngestionResult; draft: TargetProfileDraft }
  | { status: "error"; query: string; message: string; candidates?: ResolvedTarget[] };

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

function App() {
  const [targetInput, setTargetInput] = useState("");
  const [health, setHealth] = useState<HealthState>({ status: "checking" });
  const [debugState, setDebugState] = useState<TargetProfileDebugState>({ status: "idle" });

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

  async function handleTargetProfileDebug(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (!trimmedTarget || debugState.status === "loading") {
      return;
    }

    setDebugState({ status: "loading", query: trimmedTarget });

    try {
      const response = await fetch(`${API_BASE_URL}/targets/resolve?query=${encodeURIComponent(trimmedTarget)}`);

      if (!response.ok) {
        throw await readTargetProfileDebugError(response);
      }

      const data = (await response.json()) as TargetIngestionResult;
      setDebugState({
        status: "success",
        query: trimmedTarget,
        data,
        draft: buildTargetProfileDraft(data)
      });
    } catch (error) {
      if (isDebugError(error)) {
        setDebugState({
          status: "error",
          query: trimmedTarget,
          message: error.message,
          candidates: error.candidates
        });
        return;
      }

      setDebugState({
        status: "error",
        query: trimmedTarget,
        message: error instanceof Error ? error.message : "Unknown TargetProfile debug error"
      });
    }
  }

  return (
    <main className="app-shell">
      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">Long-list MVP</p>
            <h1>Buyer Universe Generator</h1>
          </div>
          <div className="topbar-actions">
            <a className="debug-entry-link" href="#target-profile-debug">
              TargetProfile Debug
            </a>
            <BackendStatus health={health} />
          </div>
        </header>

        <section className="control-panel" aria-labelledby="target-form-title">
          <div className="panel-copy">
            <h2 id="target-form-title">Start a buyer universe run</h2>
            <p>
              Enter a US-listed ticker or exact company name. The current debug path runs
              target resolution and source ingestion, then renders the TargetProfile draft
              inputs for inspection.
            </p>
          </div>

          <form className="target-form" onSubmit={handleTargetProfileDebug}>
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
              <button type="submit" disabled={!trimmedTarget || debugState.status === "loading"}>
                {debugState.status === "loading" ? "Building" : "Debug Profile"}
              </button>
            </div>
          </form>
        </section>

        <section className="status-grid" aria-label="Implementation status">
          <StatusTile label="Target Feature Extractor" value="Debug Path Ready" tone="ready" />
          <StatusTile label="Buyer Candidate Retriever" value="Planned" tone="pending" />
          <StatusTile label="Evidence Store" value="Foundation Ready" tone="ready" />
          <StatusTile label="API Health" value={health.status === "online" ? "Online" : "Checking"} tone="ready" />
        </section>

        <TargetProfileDebugPanel state={debugState} />
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

function StatusTile({
  label,
  value,
  tone
}: {
  label: string;
  value: string;
  tone: "ready" | "pending";
}) {
  return (
    <div className={`status-tile status-tile--${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function TargetProfileDebugPanel({ state }: { state: TargetProfileDebugState }) {
  return (
    <section className="debug-panel" id="target-profile-debug" aria-labelledby="target-profile-debug-title">
      <div className="debug-panel__header">
        <div>
          <p className="eyebrow">Debug</p>
          <h2 id="target-profile-debug-title">TargetProfile builder</h2>
        </div>
        <span className={`debug-state debug-state--${state.status}`}>{state.status}</span>
      </div>

      {state.status === "idle" && (
        <div className="debug-empty">
          Run a debug profile from the target input above.
        </div>
      )}

      {state.status === "loading" && (
        <div className="debug-empty">
          Resolving and ingesting public source metadata for {state.query}.
        </div>
      )}

      {state.status === "error" && (
        <DebugError message={state.message} candidates={state.candidates} />
      )}

      {state.status === "success" && <DebugResult state={state} />}
    </section>
  );
}

function DebugError({
  message,
  candidates
}: {
  message: string;
  candidates?: ResolvedTarget[];
}) {
  return (
    <div className="debug-error">
      <strong>{message}</strong>
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

function DebugResult({
  state
}: {
  state: Extract<TargetProfileDebugState, { status: "success" }>;
}) {
  const { data, draft } = state;

  return (
    <div className="debug-result">
      {data.warnings.length > 0 && (
        <div className="debug-warning-list" aria-label="Source warnings">
          {data.warnings.map((warning) => (
            <span key={warning}>{warning}</span>
          ))}
        </div>
      )}

      <div className="debug-section">
        <h3>Resolved Target</h3>
        <dl className="debug-metrics">
          <DebugMetric label="Name" value={data.target.canonical_name} />
          <DebugMetric label="Ticker" value={data.target.ticker} />
          <DebugMetric label="CIK" value={data.target.cik} />
          <DebugMetric label="Exchange" value={data.target.exchange ?? "N/A"} />
          <DebugMetric label="SIC" value={data.target.sic ?? "N/A"} />
          <DebugMetric label="Confidence" value={formatPercent(data.target.resolution_confidence)} />
        </dl>
      </div>

      <div className="debug-section">
        <h3>TargetProfile Draft</h3>
        <div className="debug-json-grid">
          <pre>{JSON.stringify(draft, null, 2)}</pre>
          <div className="debug-field-list">
            <strong>Pending extractor fields</strong>
            {draft.pending_fields.map((field) => (
              <span key={field}>{field}</span>
            ))}
          </div>
        </div>
      </div>

      <div className="debug-section">
        <h3>Filings</h3>
        <div className="debug-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Form</th>
                <th>Filing date</th>
                <th>Period</th>
                <th>Accession</th>
              </tr>
            </thead>
            <tbody>
              {data.filings.map((filing) => (
                <tr key={`${filing.form}-${filing.accession_number}`}>
                  <td>{filing.form}</td>
                  <td>{filing.filing_date ?? "N/A"}</td>
                  <td>{filing.period_of_report ?? "N/A"}</td>
                  <td>
                    {filing.url ? (
                      <a href={filing.url} target="_blank" rel="noreferrer">
                        {filing.accession_number}
                      </a>
                    ) : (
                      filing.accession_number
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="debug-section">
        <h3>Source Documents</h3>
        <div className="debug-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Source</th>
                <th>Type</th>
                <th>Strength</th>
                <th>Retrieved</th>
                <th>Reference</th>
              </tr>
            </thead>
            <tbody>
              {data.source_documents.map((document, index) => (
                <tr key={`${document.source_id}-${document.filing_accession ?? document.url ?? index}`}>
                  <td>{document.source_id}</td>
                  <td>{document.source_type}</td>
                  <td>{document.source_strength}</td>
                  <td>{formatDate(document.retrieved_at)}</td>
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
            </tbody>
          </table>
        </div>
      </div>

      <details className="debug-raw">
        <summary>Raw API response</summary>
        <pre>{JSON.stringify(data, null, 2)}</pre>
      </details>
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

function buildTargetProfileDraft(data: TargetIngestionResult): TargetProfileDraft {
  const polygonDocument = data.source_documents.find((document) => document.source_id === "polygon");
  const polygonMetadata = polygonDocument?.metadata ?? {};
  const businessSummary = stringField(polygonMetadata, "description");
  const sizeMetrics = sizeMetricFields(polygonMetadata);

  const featureLabels: TargetProfileDraft["feature_labels"] = {
    target_id: "verified_fact",
    name: "verified_fact",
    ticker: "verified_fact",
    cik: "verified_fact"
  };

  if (data.target.exchange) {
    featureLabels.exchange = "verified_fact";
  }
  if (data.target.sic) {
    featureLabels.sic = "verified_fact";
  }
  if (businessSummary) {
    featureLabels.business_summary = "verified_fact";
  }
  if (Object.keys(sizeMetrics).length > 0) {
    featureLabels.size_metrics = "verified_fact";
  }

  return {
    target_id: data.target.cik,
    name: data.target.canonical_name,
    ticker: data.target.ticker,
    cik: data.target.cik,
    exchange: data.target.exchange,
    sic: data.target.sic,
    business_summary: businessSummary,
    products: [],
    customer_segments: [],
    channels: [],
    geographies: [],
    size_metrics: sizeMetrics,
    keywords: [],
    adjacent_categories: [],
    feature_labels: featureLabels,
    pending_fields: [
      "products",
      "customer_segments",
      "channels",
      "geographies",
      "keywords",
      "adjacent_categories",
      "evidence"
    ],
    evidence_source_count: data.source_documents.length
  };
}

function stringField(metadata: Record<string, unknown>, key: string): string | null {
  const value = metadata[key];
  return typeof value === "string" && value.trim() ? value : null;
}

function sizeMetricFields(metadata: Record<string, unknown>): Record<string, string | number> {
  const metricKeys = [
    "market_cap",
    "weighted_shares_outstanding",
    "share_class_shares_outstanding",
    "total_employees",
    "employee_count"
  ];

  return metricKeys.reduce<Record<string, string | number>>((metrics, key) => {
    const value = metadata[key];
    if (typeof value === "string" || typeof value === "number") {
      metrics[key] = value;
    }
    return metrics;
  }, {});
}

async function readTargetProfileDebugError(response: Response): Promise<Error & { candidates?: ResolvedTarget[] }> {
  try {
    const payload = (await response.json()) as {
      detail?: string | { message?: string; candidates?: ResolvedTarget[] };
    };
    const detail = payload.detail;
    if (typeof detail === "string") {
      return Object.assign(new Error(detail), {});
    }

    return Object.assign(new Error(detail?.message ?? `Backend returned HTTP ${response.status}`), {
      candidates: detail?.candidates
    });
  } catch {
    return Object.assign(new Error(`Backend returned HTTP ${response.status}`), {});
  }
}

function isDebugError(error: unknown): error is Error & { candidates?: ResolvedTarget[] } {
  return error instanceof Error;
}

function formatPercent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return date.toLocaleString();
}

export default App;
