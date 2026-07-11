import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import type { NodeStats, QueryResponse, SampleQuery } from './api';
import { SignalPath } from './SignalPath';
import './App.css';

export default function App() {
  const [samples, setSamples] = useState<SampleQuery[]>([]);
  const [sql, setSql] = useState('');
  const [result, setResult] = useState<QueryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [runId, setRunId] = useState(0);
  const [stats, setStats] = useState<NodeStats[] | null>(null);
  const [apiUp, setApiUp] = useState<boolean | null>(null);
  const [history, setHistory] = useState<{ tier: string; ms: number }[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    api
      .health()
      .then(() => setApiUp(true))
      .catch(() => setApiUp(false));
    api.samples().then((s) => {
      setSamples(s);
      if (s.length) setSql(s[0].sql);
    });
  }, []);

  const refreshStats = () => {
    api.stats().then((r) => setStats(r.nodes)).catch(() => {});
  };

  useEffect(() => {
    refreshStats();
  }, []);

  async function runQuery(sqlToRun: string) {
    setPending(true);
    setError(null);
    try {
      const r = await api.query(sqlToRun);
      setResult(r);
      setRunId((n) => n + 1);
      setHistory((h) => [{ tier: r.tier_hit, ms: r.latency_ms }, ...h].slice(0, 12));
      refreshStats();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'query failed');
      setResult(null);
    } finally {
      setPending(false);
    }
  }

  function onEditorKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!pending && sql.trim()) runQuery(sql);
    }
  }

  return (
    <div className="page">
      <header className="header">
        <div className="header__brand">
          <span className="header__mark" aria-hidden>
            ▣
          </span>
          <span className="header__title">cacheopt</span>
        </div>
        <p className="header__tagline">
          cost-based, distributed cache-aware query optimizer — three-tier routing over a live
          DuckDB warehouse
        </p>
        <div className={`header__status ${apiUp ? 'is-up' : apiUp === false ? 'is-down' : ''}`}>
          <span className="header__status-dot" />
          {apiUp === null ? 'connecting…' : apiUp ? 'engine online' : 'engine unreachable'}
        </div>
      </header>

      <main className="layout">
        <section className="hero">
          <h1 className="hero__title">
            Every query takes <span className="hero__accent">one of three paths.</span>
          </h1>
          <p className="hero__sub">
            Run a query below and watch which tier answers it — then run it again to see how much
            faster it lands once the result is warm.
          </p>
          <SignalPath result={result} pending={pending} runId={runId} />
        </section>

        <section className="console" aria-label="query console">
          <div className="console__chips">
            {samples.map((s) => (
              <button
                key={s.name}
                className={`chip ${sql === s.sql ? 'chip--active' : ''}`}
                onClick={() => setSql(s.sql)}
                type="button"
              >
                {s.name}
              </button>
            ))}
          </div>

          <div className="console__editor">
            <textarea
              ref={textareaRef}
              value={sql}
              onChange={(e) => setSql(e.target.value)}
              onKeyDown={onEditorKeyDown}
              spellCheck={false}
              rows={6}
              placeholder="SELECT …"
              aria-label="SQL query"
            />
            <div className="console__editor-footer">
              <span className="console__hint">
                read-only SELECT · Enter to run · Shift+Enter for newline
              </span>
              <button
                className="run-btn"
                type="button"
                disabled={pending || !sql.trim()}
                onClick={() => runQuery(sql)}
              >
                {pending ? 'Running…' : 'Run query'}
              </button>
            </div>
          </div>

          {error && (
            <div className="alert" role="alert">
              <strong>Query rejected.</strong> {error}
            </div>
          )}

          {result && !error && (
            <div className="result">
              <div className="result__meta">
                <span>
                  <strong>{result.row_count}</strong> row{result.row_count === 1 ? '' : 's'}
                </span>
                <span className="dot">·</span>
                <span>
                  answered by <strong>{result.node_id}</strong>
                </span>
                {result.rewrites_applied.length > 0 && (
                  <>
                    <span className="dot">·</span>
                    <span>rewrites: {result.rewrites_applied.join(', ')}</span>
                  </>
                )}
              </div>
              <div className="result__reason">{result.routing_reason}</div>
              <div className="result__table-wrap">
                <table className="result__table">
                  <thead>
                    <tr>
                      {result.columns.map((c) => (
                        <th key={c}>{c}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {result.rows.map((row, i) => (
                      <tr key={i}>
                        {row.map((cell, j) => (
                          <td key={j}>{cell === null ? '—' : String(cell)}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
                {result.truncated && (
                  <p className="result__truncated">
                    showing first 200 of {result.row_count} rows
                  </p>
                )}
              </div>
            </div>
          )}
        </section>

        <section className="side">
          {history.length > 0 && (
            <div className="panel">
              <h2 className="panel__title">Recent runs</h2>
              <ul className="history">
                {history.map((h, i) => (
                  <li key={i} className={`history__item history__item--${h.tier}`}>
                    <span className="history__tier">{h.tier.replace('_', ' ')}</span>
                    <span className="history__ms">{h.ms.toFixed(2)} ms</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {stats && (
            <div className="panel">
              <h2 className="panel__title">Fleet stats</h2>
              <div className="nodes">
                {stats.map((n) => (
                  <div className="node" key={n.node_id}>
                    <div className="node__id">{n.node_id}</div>
                    <div className="node__row">
                      <span>L1 hit rate</span>
                      <span>{(n.cache.l1.hit_rate * 100).toFixed(0)}%</span>
                    </div>
                    <div className="node__row">
                      <span>L2 hit rate</span>
                      <span>{(n.cache.l2.hit_rate * 100).toFixed(0)}%</span>
                    </div>
                    <div className="node__row">
                      <span>templates tracked</span>
                      <span>{n.templates_tracked}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>
      </main>

      <footer className="footer">
        <span>cacheopt — L1 in-process buffer · L2 Redis · L3 DuckDB</span>
      </footer>
    </div>
  );
}
