# Berserk KQL performance guide for tool authors

This guide is the checklist for adding or changing a fixed query in
`berserk-mcp`. It describes Berserk's execution model, not Azure Data Explorer's
rules of thumb. The authoritative references are Berserk's [best practices]
(https://docs.bzrk.dev/docs/query-language/best-practices/), [query language
guide](https://docs.bzrk.dev/docs/query-language/), and [Microsoft KQL
comparison](https://docs.bzrk.dev/docs/query-language/compared-to-microsoft-kql/).

## The contract every query must satisfy

- **Bound both axes.** Pass a narrow, explicit `since` default and end the KQL
  with an explicit bound: `take`, `tail`, `top`, `count`, or `summarize`. Berserk
  inserts an implicit `take 2000` only when a query has no limiting operator;
  relying on that default makes a future engine change a silent availability
  regression.
- **Filter first.** Put timestamp and selective service/metric predicates before
  projections, parsing, expansion, joins, or aggregation.
- **Project only what the consumer uses.** `body`, `$raw`, `resource`, and
  `attributes` can be very wide. A 150 KB body multiplied by 2,000 rows is a
  transfer and serialization problem even when the engine scan is fast. Prefer
  a bounded `substring`, `strlen`, or key-only projection.
- **Push filtering and aggregation down; pull cosmetic work up.** Use Berserk's
  indexes and `summarize` to reduce rows. Sort or format a small result in
  Python when the order is not part of the query's semantics.
- **Preserve order deliberately.** Removing `sort` is not safe if a consumer
  builds adjacency, phases, gaps, or sequences from input order. Either keep a
  KQL order, use `tail` for recency, or sort explicitly in Python before walking
  the rows.

## What Berserk can prune

Berserk uses several index layers before row evaluation:

1. **Shard indexes** skip whole chunks for common dimensions, typically
   `resource['service.name']` and `metric_name`.
2. **Range indexes** prune native `timestamp` and numeric/datetime ranges.
3. **Bloom/column indexes** prune string and dynamic values for `has`,
   `contains`, `startswith`, `endswith`, `==`, `=~`, `search`, and wildcard
   predicates. Choose an operator for its meaning; the ADX rule that `has` is
   categorically cheaper than `contains` does not apply here.

Dynamic paths are first-class indexed values. Prefer:

```kql
| where resource['service.name'] == 'checkout'
| where attributes.http.status_code >= 500
```

Do not cast just to read a dynamic field. Berserk compares stored native types
and injects the `asXXX` extractors when a typed function needs one. Explicit
`to*()` conversions belong in `project`/`extend`, especially when crossing a
real type boundary. The planner can see through some safe wrappers, including
case folding and identity `tostring`, but a bare path is the clearest and most
portable form. Check `--stats` when a predicate matters: a physical plan with
`shar`, `range`, or `bloom` skips confirms pruning.

## Recency, ordering, and limits

- Use `tail N` for “most recent N”. Berserk uses indexes to find the newest
  rows; `sort by timestamp desc | take N` materializes the window first.
- Use `take N` for an arbitrary sample where order does not matter.
- Use `top N by value` for a ranked result.
- Keep a KQL sort only when the result contract requires it and the input has
  already been reduced. For a bounded session timeline, filter by session first
  and then sort; for a cross-session workflow feed, sort in Python only if the
  analyzer explicitly restores chronological order.

## Native engine substitutions

Prefer engine-side operations that return a small result:

- `fieldstats` (with `take N` or `with limit=N depth=M`) for dynamic-field
  discovery instead of exporting raw samples just to inspect keys.
- `extract_log_template`, `log_template_id`, and `log_template_hash` for
  grouping repeated log shapes instead of pulling thousands of bodies into
  Python.
- `rate`, `deriv`, `otel_rate`, and histogram aggregates for time-series rates
  and percentiles instead of client-side counting.
- `summarize ... by ...` for counts and rollups. Filter both sides before a
  join; use `in()` for a single-column membership filter.
- Give regex a literal anchor, for example
  `matches regex 'GET /api/v[0-9]+'`. Anchorless patterns have no bloom token
  to mine and degrade to a scan.

Do not generate ADX-only features: `materialize()`, materialized views, stored
functions, control commands beyond Berserk's supported `.show` forms,
cross-cluster queries, join hints, or `lookup`.

## Fleet tenancy

Many independent MCP processes may share one cluster. A polite tool therefore:

- uses narrow defaults and explicit limits on every call;
- adds random startup jitter (up to 7,200 seconds by the measured default) in worker/cron mode and avoids
  synchronized cron minutes;
- fails fast on a timeout with guidance to narrow the window; it does not
  immediately retry the same query in a loop;
- batches catalog/drift work and caches stable `getschema`/rollup results for a
  short TTL (about 30–60 seconds where stale data is acceptable);
- avoids sequential per-service round trips when one grouped query can answer
  the question; and
- returns aggregates or narrow excerpts rather than exporting raw rows.

## Verification checklist

Before merging a new or changed query:

1. Run it against a live deployment and capture `--stats` for the intended
   window. Confirm shard/range/bloom skips and a bounded row count.
2. Test a representative wide-body row. Assert that the query does not project
   raw `body`/bags unless the consumer truly needs them.
3. Identify every Python consumer and prove whether it depends on input order.
4. Add a unit test that locks the important KQL shape and a pure-function test
   for the preserved behavior.
5. Run `python3 -m unittest discover -s tests` before and after the change.

### Deployment evidence (2026-07-23)

On the configured `homelab` profile, a one-hour `claude-code` count showed
586 shard chunks skipped and 260 bloom chunks skipped; a timestamp-bounded
count used range pruning. `tail 50` and `sort ... | take 50` returned the same
rows; the captured engine times were 0.12s and 0.14s respectively. The
wrapped `tostring(resource['service.name'])` predicate had the same physical
skip profile as the bare predicate in this deployment. Re-run these checks when
the cluster's indexing configuration changes.

### Verified function availability (2026-07-23)

These are bounded live probes against the configured `homelab` profile using a
15-minute event-time window. “Available” means the query parsed and returned a
result; the semantic-top probe is recorded as unavailable because the parser
rejected the documented modifier.

| Feature | Result | Probe/evidence |
|---|---|---|
| `tail` | Available | `default \| tail 1` returned the newest row. |
| `make-series` | Available | Five-minute count series returned three buckets. |
| `series_fit_line` | Available | Returned fit coefficients for the count series. |
| `series_decompose_anomalies` | Available | Returned anomaly, score, and baseline arrays. |
| `series_fir` | Available | Returned the filtered count series. |
| `rate` | Available | `rate($raw.max, timestamp)` returned a numeric aggregate. |
| `deriv` | Available | `deriv($raw.max, timestamp)` returned a numeric aggregate. |
| `bin_auto` | Available | `summarize ... by bin_auto(timestamp)` returned a bucket. |
| `extract_log_template` | Available | `extract_log_template(tostring($raw))` returned a template. |
| `fieldstats` | Available | `fieldstats $raw with limit=3 depth=1` returned field metadata. |
| `top ... similarto` | Not available in current parser | `top 1 by metric_name similarto "timeout"` failed with a parse error at `similarto`. |
| Multi-statement `;` | Available | `default \| count; default \| take 1` returned two result tables. |

### Phase 1 native-query timings (2026-07-23)

Read-only live comparisons use the same `homelab` profile and bounded window;
the wall-clock column includes CLI and transport overhead, while engine time is
the `--stats` query-time value. `make-series` intentionally returns compact
zero-filled arrays rather than one row per bucket.

| Upgrade | Before (engine / wall) | After (engine / wall) | Result |
|---|---:|---:|---|
| Error-rate rollup → `make-series` | 0.25s / 0.59s | 0.20s / 0.53s | Same one-service result; zero-filled series shape. |
| Log-spike rollup → `make-series` | 0.42s / 0.78s | 0.65s / 1.33s | Zero-filled series; modest overhead is the cost of preserving empty buckets. |
| Error grouping → `extract_log_template` | 0.14s / 0.49s | 0.11s / 0.54s | Variable-bearing messages cluster under one template with an example line. |
| Recent severity rows → `tail` | 0.09s / 0.40s | 0.10s / 0.39s | Recency preserved; `timestamp` remains available before `tail`. |
| Host memory predicate (wrapped → bare path) | 0.14s / 0.48s | 0.22s / 0.54s | Counts matched (119 rows each); timing variance favored the wrapped run in this sample. |
| Schema keys → `fieldstats` | 11.21s / 11.55s | 0.07s / 0.35s | Type/cardinality metadata replaces a full `bag_keys` expansion. |
| Daily burn fit (1-day window) | 5.94s / 6.30s | 5.82s / 6.52s | Native `series_fit_line` returned seven fit values including R² and slope. |

The bare-path sweep changed the `attributes['state'] == 'used'` predicates in
host-memory queries. Claude error/tool-name and body predicates retain their
`tostring` wrappers: a live bare comparison for `attributes['claude.error']`
returned no rows, so removing the coercion would change semantics rather than
improve pruning.
