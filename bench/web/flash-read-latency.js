// Bounded aggregate timing distribution. No raw packet data or per-packet log is retained.
const LIMITS = [5, 20, 50, 80, 120, 200, 500];
const LABELS = ['≤5', '≤20', '≤50', '≤80', '≤120', '≤200', '≤500', '>500'];

export function latencyStats() {
  return { count: 0, sum_ms: 0, min_ms: null, max_ms: null, buckets: Array(8).fill(0) };
}

export function observeLatency(stats, ms) {
  if (!Number.isFinite(ms) || ms < 0) return;
  stats.count++;
  stats.sum_ms += ms;
  stats.min_ms = stats.min_ms === null ? ms : Math.min(stats.min_ms, ms);
  stats.max_ms = stats.max_ms === null ? ms : Math.max(stats.max_ms, ms);
  const i = LIMITS.findIndex(limit => ms <= limit);
  stats.buckets[i === -1 ? LIMITS.length : i]++;
}

export function mergeLatency(target, source) {
  if (!source) return;
  target.count += source.count;
  target.sum_ms += source.sum_ms;
  if (source.min_ms !== null) target.min_ms = target.min_ms === null ? source.min_ms : Math.min(target.min_ms, source.min_ms);
  if (source.max_ms !== null) target.max_ms = target.max_ms === null ? source.max_ms : Math.max(target.max_ms, source.max_ms);
  for (let i = 0; i < target.buckets.length; i++) target.buckets[i] += source.buckets[i];
}

export function summarizeLatency(stats) {
  const rounded = value => value === null ? null : Math.round(value * 10) / 10;
  const percentileBucket = fraction => {
    if (!stats.count) return null;
    const threshold = Math.ceil(stats.count * fraction);
    let cumulative = 0;
    for (let i = 0; i < stats.buckets.length; i++) {
      cumulative += stats.buckets[i];
      if (cumulative >= threshold) return LABELS[i];
    }
    return LABELS.at(-1);
  };
  return { count: stats.count,
    mean_ms: stats.count ? rounded(stats.sum_ms / stats.count) : null,
    min_ms: rounded(stats.min_ms), max_ms: rounded(stats.max_ms),
    p50_bucket_ms: percentileBucket(0.50), p95_bucket_ms: percentileBucket(0.95),
    histogram_ms: Object.fromEntries(LABELS.map((label, i) => [label, stats.buckets[i]])) };
}
