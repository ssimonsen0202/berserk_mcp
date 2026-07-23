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
- adds random startup jitter (0–300 seconds) in worker/cron mode and avoids
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
