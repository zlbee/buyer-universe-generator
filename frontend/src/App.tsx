import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";

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

type CandidateHit = {
  candidate_name: string;
  candidate_ticker: string | null;
  candidate_cik: string | null;
  candidate_domain: string | null;
  buyer_type: "strategic" | "financial";
  retriever_name: string;
  source_path: string[];
  fit_reason: string;
  evidence: Evidence[];
  confidence: number;
  pending_verification: boolean;
  retrieval_metadata: Record<string, unknown>;
};

type CandidateRetrievalResultData = {
  target_profile: TargetProfile;
  hits: CandidateHit[];
  warnings: string[];
  metadata: Record<string, unknown>;
};

type TargetProfileDebugState =
  | { status: "idle" }
  | { status: "loading"; query: string }
  | { status: "success"; query: string; data: TargetProfileExtractionResult }
  | { status: "error"; query: string; message: string; errorCode?: string; candidates?: ResolvedTarget[] };

type CandidateRetrievalState =
  | { status: "idle" }
  | { status: "loading"; query: string }
  | { status: "success"; query: string; data: CandidateRetrievalResultData }
  | { status: "error"; query: string; message: string; errorCode?: string; candidates?: ResolvedTarget[] };

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

function App() {
  const [targetInput, setTargetInput] = useState("ELF");
  const [health, setHealth] = useState<HealthState>({ status: "checking" });
  const [debugState, setDebugState] = useState<TargetProfileDebugState>({ status: "idle" });
  const [candidateState, setCandidateState] = useState<CandidateRetrievalState>({ status: "idle" });
  const [isDebugEnabled, setIsDebugEnabled] = useState(false);

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
        throw await readDebugError(response);
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

  async function handleCandidateRetrieval(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (!trimmedTarget || candidateState.status === "loading") {
      return;
    }

    setCandidateState({ status: "loading", query: trimmedTarget });

    try {
      const response = await fetch(`${API_BASE_URL}/buyers/candidates?query=${encodeURIComponent(trimmedTarget)}`);

      if (!response.ok) {
        throw await readDebugError(response);
      }

      const data = (await response.json()) as CandidateRetrievalResultData;
      setCandidateState({ status: "success", query: trimmedTarget, data });
    } catch (error) {
      if (isDebugError(error)) {
        setCandidateState({
          status: "error",
          query: trimmedTarget,
          message: error.message,
          errorCode: error.errorCode,
          candidates: error.candidates
        });
        return;
      }

      setCandidateState({
        status: "error",
        query: trimmedTarget,
        message: error instanceof Error ? error.message : "Unknown candidate retrieval error"
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
            <a className="debug-entry-link" href="#target-profile-card">
              TargetProfile
            </a>
            <a className="debug-entry-link" href="#buyer-recall-card">
              Buyer Recall
            </a>
            <BackendStatus health={health} />
          </div>
        </header>

        <section className="status-grid" aria-label="Implementation status">
          <StatusTile label="Target Feature Extractor" value="Phase 2 Ready" tone="ready" />
          <StatusTile label="Buyer Candidate Retriever" value="Phase 5 Ready" tone="ready" />
          <StatusTile label="Evidence Store" value="Profile Cache Ready" tone="ready" />
          <StatusTile label="API Health" value={health.status === "online" ? "Online" : "Checking"} tone="ready" />
        </section>

        <section className="page-card-grid" aria-label="Phase tools">
          <PageCard
            id="target-profile-card"
            eyebrow="Phase 2"
            title="TargetProfile Builder"
            status={debugState.status}
          >
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
              <label className="debug-toggle" htmlFor="target-debug-toggle">
                <input
                  id="target-debug-toggle"
                  type="checkbox"
                  checked={isDebugEnabled}
                  onChange={(event) => setIsDebugEnabled(event.target.checked)}
                />
                <span className="debug-toggle__control" aria-hidden="true" />
                <span>是否打开 Debug</span>
              </label>
            </form>
            <TargetProfileDebugPanel state={debugState} showFeatureEvidence={isDebugEnabled} />
          </PageCard>

          <PageCard
            id="buyer-recall-card"
            eyebrow="Phase 4/5"
            title="Potential Buyer Recaller"
            status={candidateState.status}
          >
            <form className="target-form" onSubmit={handleCandidateRetrieval}>
              <label htmlFor="buyer-target-input">Target ticker or company name</label>
              <div className="input-row">
                <input
                  id="buyer-target-input"
                  name="buyer-target"
                  value={targetInput}
                  onChange={(event) => setTargetInput(event.target.value)}
                  placeholder="e.g. ELF or e.l.f. Beauty"
                  autoComplete="off"
                />
                <button type="submit" disabled={!trimmedTarget || candidateState.status === "loading"}>
                  {candidateState.status === "loading" ? "Recalling" : "Recall Buyers"}
                </button>
              </div>
            </form>
            <CandidateRetrievalPanel state={candidateState} />
          </PageCard>
        </section>
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
  id,
  eyebrow,
  title,
  status,
  children
}: {
  id: string;
  eyebrow: string;
  title: string;
  status: TargetProfileDebugState["status"] | CandidateRetrievalState["status"];
  children: ReactNode;
}) {
  return (
    <section className="page-card" id={id} aria-labelledby={`${id}-title`}>
      <div className="page-card__header">
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h2 id={`${id}-title`}>{title}</h2>
        </div>
        <span className={`debug-state debug-state--${status}`}>{status}</span>
      </div>
      <div className="page-card__body">{children}</div>
    </section>
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

function TargetProfileDebugPanel({
  state,
  showFeatureEvidence
}: {
  state: TargetProfileDebugState;
  showFeatureEvidence: boolean;
}) {
  return (
    <div className="debug-panel">
      {state.status === "idle" && <div className="debug-empty">Build a profile from the target input above.</div>}

      {state.status === "loading" && (
        <div className="debug-empty">
          Resolving, fetching source text, and extracting profile features for {state.query}.
        </div>
      )}

      {state.status === "error" && (
        <DebugError message={state.message} errorCode={state.errorCode} candidates={state.candidates} />
      )}

      {state.status === "success" && <DebugResult state={state} showFeatureEvidence={showFeatureEvidence} />}
    </div>
  );
}

function CandidateRetrievalPanel({ state }: { state: CandidateRetrievalState }) {
  return (
    <div className="debug-panel">
      {state.status === "idle" && <div className="debug-empty">Run strategic and financial first-pass recall from the target input above.</div>}

      {state.status === "loading" && (
        <div className="debug-empty">
          Building the target profile and running strategic and financial candidate retrievers for {state.query}.
        </div>
      )}

      {state.status === "error" && (
        <DebugError message={state.message} errorCode={state.errorCode} candidates={state.candidates} />
      )}

      {state.status === "success" && <CandidateRetrievalResult state={state} />}
    </div>
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
  state,
  showFeatureEvidence
}: {
  state: Extract<TargetProfileDebugState, { status: "success" }>;
  showFeatureEvidence: boolean;
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

      {showFeatureEvidence && (
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
      )}

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

function CandidateRetrievalResult({
  state
}: {
  state: Extract<CandidateRetrievalState, { status: "success" }>;
}) {
  const { data } = state;
  const profile = data.target_profile;

  return (
    <div className="debug-result">
      {data.warnings.length > 0 && (
        <div className="debug-warning-list" aria-label="Retrieval warnings">
          {data.warnings.map((warning) => (
            <span key={warning}>{warning}</span>
          ))}
        </div>
      )}

      <div className="debug-section">
        <h3>Recall Summary</h3>
        <dl className="debug-metrics">
          <DebugMetric label="Target" value={profile.ticker} />
          <DebugMetric label="SIC" value={profile.sic ?? "N/A"} />
          <DebugMetric label="Raw Hits" value={String(data.hits.length)} />
        </dl>
      </div>

      <div className="debug-section">
        <h3>Candidate Hits</h3>
        {data.hits.length === 0 ? (
          <div className="debug-empty">No buyer candidate hits returned.</div>
        ) : (
          <div className="debug-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Candidate</th>
                  <th>Type</th>
                  <th>Retriever</th>
                  <th>Path</th>
                  <th>Confidence</th>
                  <th>Evidence</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {data.hits.map((hit, index) => (
                  <tr key={`${hit.candidate_name}-${hit.retriever_name}-${index}`}>
                    <td>
                      <strong>{hit.candidate_name}</strong>
                      <span className="table-subtext">
                        {[hit.candidate_ticker, hit.candidate_cik].filter(Boolean).join(" · ") || "No ticker/CIK"}
                      </span>
                    </td>
                    <td>{hit.buyer_type}</td>
                    <td>{hit.retriever_name}</td>
                    <td>{hit.source_path.join(", ") || "N/A"}</td>
                    <td>{formatConfidence(hit.confidence)}</td>
                    <td>{hit.evidence.length}</td>
                    <td>{hit.fit_reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <details className="debug-raw">
        <summary>Retrieval metadata</summary>
        <pre>{JSON.stringify(data.metadata, null, 2)}</pre>
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

function formatConfidence(value: number) {
  return `${Math.round(value * 100)}%`;
}

async function readDebugError(
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
