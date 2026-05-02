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

type TargetProfileDebugState =
  | { status: "idle" }
  | { status: "loading"; query: string }
  | { status: "success"; query: string; data: TargetProfileExtractionResult }
  | { status: "error"; query: string; message: string; errorCode?: string; candidates?: ResolvedTarget[] };

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

function App() {
  const [targetInput, setTargetInput] = useState("ELF");
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
      const response = await fetch(`${API_BASE_URL}/targets/profile?query=${encodeURIComponent(trimmedTarget)}`);

      if (!response.ok) {
        throw await readTargetProfileDebugError(response);
      }

      const data = (await response.json()) as TargetProfileExtractionResult;
      setDebugState({ status: "success", query: trimmedTarget, data });
    } catch (error) {
      if (isDebugError(error)) {
        setDebugState({
          status: "error",
          query: trimmedTarget,
          message: error.message,
          errorCode: error.errorCode,
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
              Enter a US-listed ticker or exact company name. The current debug path resolves
              the target, fetches source text, runs required LLM extraction, and renders the
              evidence-backed TargetProfile.
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
                {debugState.status === "loading" ? "Building" : "Build Profile"}
              </button>
            </div>
          </form>
        </section>

        <section className="status-grid" aria-label="Implementation status">
          <StatusTile label="Target Feature Extractor" value="Phase 2 Ready" tone="ready" />
          <StatusTile label="Buyer Candidate Retriever" value="Planned" tone="pending" />
          <StatusTile label="Evidence Store" value="Profile Cache Ready" tone="ready" />
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

      {state.status === "idle" && <div className="debug-empty">Build a profile from the target input above.</div>}

      {state.status === "loading" && (
        <div className="debug-empty">
          Resolving, fetching source text, and extracting profile features for {state.query}.
        </div>
      )}

      {state.status === "error" && (
        <DebugError message={state.message} errorCode={state.errorCode} candidates={state.candidates} />
      )}

      {state.status === "success" && <DebugResult state={state} />}
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

function DebugResult({
  state
}: {
  state: Extract<TargetProfileDebugState, { status: "success" }>;
}) {
  const { data } = state;
  const profile = data.target_profile;

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
          <DebugMetric label="Name" value={profile.name} />
          <DebugMetric label="Ticker" value={profile.ticker} />
          <DebugMetric label="CIK" value={profile.cik} />
          <DebugMetric label="Exchange" value={profile.exchange ?? "N/A"} />
          <DebugMetric label="SIC" value={profile.sic ?? "N/A"} />
          <DebugMetric label="Evidence" value={String(profile.evidence.length)} />
        </dl>
      </div>

      <div className="debug-section">
        <h3>TargetProfile</h3>
        <div className="debug-json-grid">
          <pre>{JSON.stringify(profile, null, 2)}</pre>
          <div className="debug-field-list">
            <strong>Feature labels</strong>
            {Object.entries(profile.feature_labels).map(([field, label]) => (
              <span key={field}>
                {field}: {label}
              </span>
            ))}
          </div>
        </div>
      </div>

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
              {Object.entries(profile.feature_evidence).flatMap(([field, evidenceItems]) =>
                evidenceItems.map((evidence, index) => (
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
                ))
              )}
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
                <th>Dimension</th>
                <th>Type</th>
                <th>Strength</th>
                <th>Text</th>
                <th>Reference</th>
              </tr>
            </thead>
            <tbody>
              {data.source_documents.map((document, index) => (
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
            </tbody>
          </table>
        </div>
      </div>

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

function DebugMetric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

async function readTargetProfileDebugError(
  response: Response
): Promise<Error & { candidates?: ResolvedTarget[]; errorCode?: string }> {
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

function isDebugError(error: unknown): error is Error & { candidates?: ResolvedTarget[]; errorCode?: string } {
  return error instanceof Error;
}

export default App;
