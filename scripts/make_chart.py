import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

with open("benchmarks/results/summary.json") as f:
    s = json.load(f)

fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))

# 1. Latency percentiles: baseline vs optimized
labels = ["mean", "p50", "p95", "p99"]
base_vals = [s["baseline"][f"{k}_ms"] for k in ["mean", "p50", "p95", "p99"]]
opt_vals = [s["optimized"][f"{k}_ms"] for k in ["mean", "p50", "p95", "p99"]]
x = range(len(labels))
w = 0.35
ax = axes[0]
ax.bar([i - w/2 for i in x], base_vals, width=w, label="Baseline (no cache)", color="#c0392b")
ax.bar([i + w/2 for i in x], opt_vals, width=w, label="Optimized (cacheopt)", color="#2471a3")
ax.axhline(150, color="gray", linestyle="--", linewidth=1, label="150ms target")
ax.set_xticks(list(x)); ax.set_xticklabels(labels)
ax.set_ylabel("latency (ms)")
ax.set_title("Latency: baseline vs optimized")
ax.legend(fontsize=8)

# 2. Repeat-workload latency reduction
ax2 = axes[1]
rw = s["repeat_workload"]
ax2.bar(["baseline", "optimized"], [rw["baseline_mean_ms"], rw["optimized_mean_ms"]],
        color=["#c0392b", "#27ae60"])
ax2.set_ylabel("mean latency (ms)")
ax2.set_title(f"Repeat-workload latency\n({rw['latency_reduction_pct']:.1f}% reduction, n={rw['n_repeat_queries']})")

# 3. Cache tier distribution
ax3 = axes[2]
tiers = s["cache_tier_distribution"]
colors = {"L1_MEMORY": "#27ae60", "L2_REDIS": "#f39c12", "MISS": "#c0392b"}
labels3 = list(tiers.keys())
values3 = [tiers[k] for k in labels3]
ax3.pie(values3, labels=labels3, autopct="%1.0f%%", colors=[colors.get(k, "#999") for k in labels3])
ax3.set_title(f"Cache tier hits (n={s['n_queries']} queries)")

plt.tight_layout()
plt.savefig("benchmarks/results/benchmark_chart.png", dpi=130)
print("chart saved")
