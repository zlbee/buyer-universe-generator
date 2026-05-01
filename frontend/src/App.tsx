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

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

function App() {
  const [targetInput, setTargetInput] = useState("");
  const [health, setHealth] = useState<HealthState>({ status: "checking" });

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

  return (
    <main className="app-shell">
      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">Long-list MVP</p>
            <h1>Buyer Universe Generator</h1>
          </div>
          <BackendStatus health={health} />
        </header>

        <section className="control-panel" aria-labelledby="target-form-title">
          <div className="panel-copy">
            <h2 id="target-form-title">Start a buyer universe run</h2>
            <p>
              Enter a US-listed ticker or exact company name. Phase 0 wires the interface
              and backend health path; target resolution and candidate retrieval arrive in
              the next milestones.
            </p>
          </div>

          <form className="target-form">
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
              <button type="button" disabled={!trimmedTarget}>
                Prepare Run
              </button>
            </div>
          </form>
        </section>

        <section className="status-grid" aria-label="Implementation status">
          <StatusTile label="Target Feature Extractor" value="Planned" tone="pending" />
          <StatusTile label="Buyer Candidate Retriever" value="Planned" tone="pending" />
          <StatusTile label="Evidence Store" value="Foundation Ready" tone="ready" />
          <StatusTile label="API Health" value={health.status === "online" ? "Online" : "Checking"} tone="ready" />
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

export default App;

