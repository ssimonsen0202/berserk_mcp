# Berserk janitor config schema note (2026-07-27)

This note captures what was learned while triaging the `beserk` cluster during
berserk-mcp v1.23 live validation.

## Incident summary

During v1.23 MCP live testing, cluster-wide query tools began timing out. The MCP
code was not the cause. The `berserk-janitor-1` container
(`ghcr.io/berserkdb/janitor:v1.0.115`) was running continuous large segment-merge
compaction, saturating the single 5400rpm HDD backing all VMs on the ESXi host.

Observed impact:

- host iowait reached 84.6%;
- load average reached 16-21 on a 2-vCPU VM;
- query reads from MinIO timed out while janitor was rewriting segment batches;
- MinIO itself logged no matching service errors.

The effect was query-engine starvation from shared disk I/O, not an MCP regression
and not the idle-worker wedge described in the earlier 2026-07-26 data-plane
timeout triage note.

## Schema finding

The compose file's inline `janitor-config` block, mounted at `/config/config.yaml`,
had been using this structure:

```yaml
merger:
  interval_seconds: 300
```

That structure is invalid for this janitor binary. It silently did nothing. The
binary does not have a `merger:` config object for these fields; it expects flat
top-level keys.

The effective schema was confirmed from the janitor startup log. On startup,
`bzrk_config::service` prints the fully parsed configuration as one `INFO` line per
field. That effective-config dump is the authoritative verification source.

Correct shape:

```yaml
merger_interval_seconds: 300
max_segments_per_batch: 100
min_segments: 2
max_segment_age_days: 7
target_tables: []
min_total_size: null
hash_tracker_memory_budget_mb: 64
max_upload_attempts: 10
writer_memory_budget: null
download_config:
  max_concurrent_downloads: 20
  max_retries: 3
  retry_backoff_secs: 2
```

Because `merger_interval_seconds` had never actually loaded, janitor was using the
built-in default of 10 seconds rather than the intended 300 seconds. That drove
continuous back-to-back compaction and exposed the storage bottleneck.

## Applied production change

Changed the deployment `docker-compose.yaml` in the inline `janitor-config`
content block:

```diff
-      merger:
-        interval_seconds: 300
+      merger_interval_seconds: 300
+      max_segments_per_batch: 100
+      download_config:
+        max_concurrent_downloads: 2
```

A backup was taken first as `docker-compose.yaml.bak.<epoch>`.

Applied with:

```bash
docker compose up -d --force-recreate janitor
```

Startup logs confirmed that `merger_interval_seconds: 300` and
`max_segments_per_batch: 100` loaded correctly.

After the change, iowait dropped from 84.6% to roughly 19-22%, load average dropped
to about 2.53 over one minute, and blocked process count dropped to 0-1. The
previously timed-out MCP validation sections passed on re-run.

## Open issue: download concurrency

`download_config.max_concurrent_downloads: 2` did not take effect through YAML for
`ghcr.io/berserkdb/janitor:v1.0.115`. The startup config dump still showed the
default value of 20 even though:

- `/config/config.yaml` inside the running container contained the nested value;
- the nested structure matched the startup config dump's own `download_config`
  shape;
- `janitor_bin --help` exposes `--max-concurrent-downloads`.

Working hypothesis: `max_concurrent_downloads` may only be settable through the CLI
flag for this binary, or there is a deserialization bug scoped to that nested
`download_config` field in janitor's config loader.

This is not currently blocking because the interval and batch-size fix resolved the
observed symptom. Revisit it if a future large merge cycle causes query starvation.

## CLI flags observed

`docker exec berserk-janitor-1 /janitor_bin --help` showed CLI flags for the same
settings:

```text
--merger-interval-seconds <MERGER_INTERVAL_SECONDS>
--max-segments-per-batch <MAX_SEGMENTS_PER_BATCH>
--min-segments <MIN_SEGMENTS>
--max-segment-age-days <MAX_SEGMENT_AGE_DAYS>
--target-tables <TARGET_TABLES>
--min-total-size <MIN_TOTAL_SIZE>
--hash-tracker-memory-budget-mb <HASH_TRACKER_MEMORY_BUDGET_MB>
--max-upload-attempts <MAX_UPLOAD_ATTEMPTS>
--writer-memory-budget <WRITER_MEMORY_BUDGET>
--max-concurrent-downloads <MAX_CONCURRENT_DOWNLOADS>
--download-max-retries <DOWNLOAD_MAX_RETRIES>
--download-retry-backoff-secs <DOWNLOAD_RETRY_BACKOFF_SECS>
```

The CLI flags use kebab-case and generally map to the snake_case YAML keys. The
known exception to confirm is `download_config.max_concurrent_downloads`.

## Verification procedure

Do not rely only on `docker compose up -d` or on the mounted file contents. One
observed failure mode was that the mounted config file changed while the running
process kept stale in-memory configuration.

Use `--force-recreate` or explicitly restart janitor, then verify the effective
runtime config from startup logs:

```bash
docker logs berserk-janitor-1 --since <restart-time> 2>&1 \
  | sed 's/\x1b\[[0-9;]*m//g' \
  | grep 'bzrk_config::service:'
```

The startup config dump is the reliable source of truth for whether a future config
change actually loaded.

## Source-level follow-up

If the janitor source is available, inspect `apps/janitor/src/...` and the
`bzrk_config` crate for the YAML deserialization path for
`download_config.max_concurrent_downloads`.

Confirm whether:

- the field is intentionally CLI-only;
- the nested YAML field name differs from the dumped structure;
- the nested struct lacks serde/default/flatten wiring;
- CLI parsing overwrites YAML defaults after file load;
- the effective config dump is printing a different field than the one used by the
  downloader.

If it is a config-loader bug, add a regression test that loads YAML with
`download_config.max_concurrent_downloads: 2` and asserts the effective janitor
config uses `2`, not the default `20`.
