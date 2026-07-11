import type { QueryResponse, TierHit } from './api';
import './SignalPath.css';

interface Lane {
  tier: TierHit;
  label: string;
  sub: string;
}

const LANES: Lane[] = [
  { tier: 'L1_MEMORY', label: 'L1 · MEMORY', sub: 'in-process LRU buffer' },
  { tier: 'L2_REDIS', label: 'L2 · REDIS', sub: 'shared across nodes' },
  { tier: 'MISS', label: 'L3 · DUCKDB', sub: 'recomputed from disk' },
];

interface Props {
  result: QueryResponse | null;
  pending: boolean;
  runId: number;
}

export function SignalPath({ result, pending, runId }: Props) {
  return (
    <div className={`signal-path ${pending ? 'signal-path--pending' : ''}`} aria-live="polite">
      {LANES.map((lane) => {
        const active = !pending && result?.tier_hit === lane.tier;
        return (
          <div className={`lane ${active ? 'lane--active' : ''}`} key={lane.tier}>
            <div className="lane__label">
              <span className="lane__name">{lane.label}</span>
              <span className="lane__sub">{lane.sub}</span>
            </div>
            <div className="lane__track">
              <div className="lane__rail" />
              {active && (
                <div className="lane__pulse" key={runId}>
                  <span className="lane__pulse-dot" />
                </div>
              )}
            </div>
            <div className="lane__readout">
              {active ? (
                <span className="lane__latency">{result!.latency_ms.toFixed(2)}<small>ms</small></span>
              ) : (
                <span className="lane__latency lane__latency--idle">—</span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
