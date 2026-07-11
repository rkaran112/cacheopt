import './Charts.css';

// Validated categorical palette (see: dataviz skill, validate_palette.js --mode dark),
// distinct from the brighter glow colors used decoratively in SignalPath.
export const TIER_COLOR: Record<string, string> = {
  L1_MEMORY: '#c98500',
  L2_REDIS: '#199e70',
  MISS: '#9085e9',
};
const TIER_LABEL: Record<string, string> = {
  L1_MEMORY: 'L1 memory',
  L2_REDIS: 'L2 Redis',
  MISS: 'L3 DuckDB',
};
const BASELINE_COLOR = '#5c6b68';

// ---------- Benchmark comparison: measured baseline vs optimized ----------
// Numbers are the reproducible, measured results in README.md (1,000-query
// workload, 88% repeat traffic, 12M-row fact table) -- not illustrative.

const BENCHMARK: { label: string; baseline: number; optimized: number }[] = [
  { label: 'mean', baseline: 31.95, optimized: 11.64 },
  { label: 'p50', baseline: 6.61, optimized: 8.59 },
  { label: 'p95', baseline: 126.24, optimized: 31.21 },
  { label: 'p99', baseline: 213.24, optimized: 89.85 },
];

export function BenchmarkChart() {
  const max = Math.max(...BENCHMARK.flatMap((d) => [d.baseline, d.optimized]));
  const chartW = 560;
  const chartH = 200;
  const groupW = chartW / BENCHMARK.length;
  const barW = 22;
  const gap = 4;

  const scaleY = (v: number) => (v / max) * (chartH - 24);

  return (
    <figure className="chart chart--benchmark">
      <figcaption className="chart__caption">
        <span className="chart__title">Latency, baseline vs cacheopt</span>
        <span className="chart__sub">
          1,000-query workload &middot; 88% repeat traffic &middot; 12M-row fact table
        </span>
      </figcaption>
      <div className="chart__legend">
        <span className="legend-item">
          <i style={{ background: BASELINE_COLOR }} /> baseline (direct DB)
        </span>
        <span className="legend-item">
          <i style={{ background: '#c98500' }} /> cacheopt
        </span>
      </div>
      <svg viewBox={`0 0 ${chartW} ${chartH + 28}`} className="chart__svg" role="img"
        aria-label="Bar chart comparing baseline and cacheopt latency across mean, p50, p95, and p99">
        <line x1={0} y1={chartH} x2={chartW} y2={chartH} className="chart__axis" />
        {BENCHMARK.map((d, i) => {
          const cx = i * groupW + groupW / 2;
          const bH = scaleY(d.baseline);
          const oH = scaleY(d.optimized);
          const bx = cx - barW - gap / 2;
          const ox = cx + gap / 2;
          return (
            <g key={d.label}>
              <rect x={bx} y={chartH - bH} width={barW} height={bH} rx={4} fill={BASELINE_COLOR} />
              <text x={bx + barW / 2} y={chartH - bH - 6} className="chart__value" textAnchor="middle">
                {d.baseline.toFixed(1)}
              </text>
              <rect x={ox} y={chartH - oH} width={barW} height={oH} rx={4} fill="#c98500" />
              <text x={ox + barW / 2} y={chartH - oH - 6} className="chart__value" textAnchor="middle">
                {d.optimized.toFixed(1)}
              </text>
              <text x={cx} y={chartH + 20} className="chart__tick" textAnchor="middle">
                {d.label}
              </text>
            </g>
          );
        })}
      </svg>
      <div className="chart__callouts">
        <div className="callout">
          <span className="callout__value">70.9%</span>
          <span className="callout__label">latency reduction on repeat queries</span>
        </div>
        <div className="callout">
          <span className="callout__value">2.74x</span>
          <span className="callout__label">overall speedup</span>
        </div>
      </div>
    </figure>
  );
}

// ---------- Live latency history: one dot per run, colored by tier ----------

interface LatencyPoint {
  tier: string;
  ms: number;
}

export function LatencyChart({ points }: { points: LatencyPoint[] }) {
  if (points.length === 0) {
    return (
      <figure className="chart chart--empty">
        <figcaption className="chart__caption">
          <span className="chart__title">Latency per run</span>
        </figcaption>
        <p className="chart__placeholder">Run a query to start the trace.</p>
      </figure>
    );
  }

  const ordered = [...points].reverse(); // chronological, oldest first
  const w = 280;
  const h = 120;
  const pad = 14;
  const max = Math.max(...ordered.map((p) => p.ms), 1);
  const stepX = ordered.length > 1 ? (w - pad * 2) / (ordered.length - 1) : 0;
  const scaleY = (ms: number) => h - pad - (ms / max) * (h - pad * 2);

  const coords = ordered.map((p, i) => ({
    x: pad + i * stepX,
    y: scaleY(p.ms),
    ...p,
  }));
  const linePath = coords.map((c, i) => `${i === 0 ? 'M' : 'L'}${c.x},${c.y}`).join(' ');

  return (
    <figure className="chart chart--latency">
      <figcaption className="chart__caption">
        <span className="chart__title">Latency per run</span>
        <span className="chart__sub">last {ordered.length} quer{ordered.length === 1 ? 'y' : 'ies'}</span>
      </figcaption>
      <svg viewBox={`0 0 ${w} ${h}`} className="chart__svg" role="img" aria-label="Line chart of query latency over recent runs, colored by which cache tier answered each one">
        <line x1={pad} y1={h - pad} x2={w - pad} y2={h - pad} className="chart__axis" />
        <path d={linePath} className="chart__line" />
        {coords.map((c, i) => (
          <circle key={i} cx={c.x} cy={c.y} r={5} fill={TIER_COLOR[c.tier] ?? BASELINE_COLOR} className="chart__dot" />
        ))}
      </svg>
      <div className="chart__legend">
        {Object.entries(TIER_LABEL).map(([tier, label]) => (
          <span className="legend-item" key={tier}>
            <i style={{ background: TIER_COLOR[tier] }} /> {label}
          </span>
        ))}
      </div>
    </figure>
  );
}

// ---------- Tier hit distribution: aggregated across the fleet ----------

export function TierDistribution({ points }: { points: LatencyPoint[] }) {
  const counts: Record<string, number> = { L1_MEMORY: 0, L2_REDIS: 0, MISS: 0 };
  for (const p of points) counts[p.tier] = (counts[p.tier] ?? 0) + 1;
  const total = points.length;

  return (
    <figure className="chart chart--dist">
      <figcaption className="chart__caption">
        <span className="chart__title">Where answers came from</span>
        <span className="chart__sub">this session &middot; {total} run{total === 1 ? '' : 's'}</span>
      </figcaption>
      {total === 0 ? (
        <p className="chart__placeholder">Run a few queries to see the split.</p>
      ) : (
        <div className="dist-bars">
          {(Object.keys(TIER_LABEL) as (keyof typeof TIER_LABEL)[]).map((tier) => {
            const n = counts[tier];
            const pct = total ? (n / total) * 100 : 0;
            return (
              <div className="dist-row" key={tier}>
                <span className="dist-row__label">{TIER_LABEL[tier]}</span>
                <div className="dist-row__track">
                  <div
                    className="dist-row__fill"
                    style={{ width: `${pct}%`, background: TIER_COLOR[tier] }}
                  />
                </div>
                <span className="dist-row__value">{n}</span>
              </div>
            );
          })}
        </div>
      )}
    </figure>
  );
}
