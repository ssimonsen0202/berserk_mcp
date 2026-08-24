# Raw evidence log — 2026-07-31 nursery/janitor incidents

Companion file to `docs/beserk-developer-meeting-briefing-2026-07-31.md`. That doc
tells the story; this one preserves the actual raw log lines the conclusions are
based on, pulled off the beserk VM before they aged out of `/tmp` there (not durable
storage — this repo is).

## Nursery: proof the original "silent stall" diagnosis was wrong

Original claim (later retracted): `berserk-nursery-1` produced zero log output of any
kind for 5+ minutes (2026-07-31T19:00:00Z–19:15:00Z) while pinned at high CPU.

Actual evidence, from a bounded `docker logs -t --since 2026-07-31T19:00:00Z --until
2026-07-31T19:15:00Z berserk-nursery-1` capture (23,252 total lines in this window):

**Per-minute line count — proves continuous activity, no gap:**

```
    618 2026-07-31T19:00
    601 2026-07-31T19:01
    626 2026-07-31T19:02
    618 2026-07-31T19:03
    615 2026-07-31T19:04
    603 2026-07-31T19:05
    609 2026-07-31T19:06
    621 2026-07-31T19:07
    609 2026-07-31T19:08
    624 2026-07-31T19:09
    615 2026-07-31T19:10
    567 2026-07-31T19:11
    573 2026-07-31T19:12
    588 2026-07-31T19:13
  14765 2026-07-31T19:14
```

(The 19:14 spike is the final minute before the restart at 19:14:18Z, coinciding with
increased merge-planning activity.)

**First lines of the window (19:00:00Z):**

```
2026-07-31T19:00:00.100105947Z   2026-07-31T19:00:00.095406Z  INFO [task_id=21] nursery::brain::cleanup_blobstore: Blobstore cleanup: all tables caught up, nothing to delete, stream_id: 019f9e88-b910-7af2-88df-1854736e7e3f, stream_merged_offset: 132274, meta_tables: 1
    at bazel-out/k8-opt/bin/libs/nursery/src/brain/cleanup_blobstore.rs:68

2026-07-31T19:00:00.597005471Z   2026-07-31T19:00:00.596856Z  INFO [task_id=21] nursery::brain::cleanup_blobstore: Blobstore cleanup: all tables caught up, nothing to delete, stream_id: 019f9e88-b910-7af2-88df-1854736e7e3f, stream_merged_offset: 132274, meta_tables: 1
    at bazel-out/k8-opt/bin/libs/nursery/src/brain/cleanup_blobstore.rs:68

2026-07-31T19:00:01.098454901Z   2026-07-31T19:00:01.098145Z  INFO [task_id=21] nursery::brain::cleanup_blobstore: Blobstore cleanup: all tables caught up, nothing to delete, stream_id: 019f9e88-b910-7af2-88df-1854736e7e3f, stream_merged_offset: 132274, meta_tables: 1
    at bazel-out/k8-opt/bin/libs/nursery/src/brain/cleanup_blobstore.rs:68

2026-07-31T19:00:01.098512221Z   2026-07-31T19:00:01.098293Z  INFO [task_id=2223639968] nursery::executor::cleanup_local_files_actions: Local cleanup: deleting offset dirs and orphan files, stream_id: 019f9e88-b910-7af2-88df-1854736e7e3f, delete_up_to_offset: 132274, delete_older_than: 2026-07-30 21:01:11 UTC
    at bazel-out/k8-opt/bin/libs/nursery/src/executor/cleanup_local_files_actions.rs:21
```

Note `delete_older_than: 2026-07-30 21:01:11 UTC` — an independent data point
corroborating the roughly-21-hour backlog age computed separately from
`time_since_ingest_secs`.

**Last lines of the window (19:14:59Z, the second before restart):**

```
2026-07-31T19:14:59.953381714Z   2026-07-31T19:14:59.953323Z  INFO [task_id=1195] segment_rewrite::segment_merger: CLUSTER 3 presence_mask=0b100011 hasher_fields_bitset=0b100011
    at libs/segment_rewrite/src/segment_merger.rs:4146
    in segment_rewrite::segment_merger::merge_planning with merge_id=019fb999-eaee-7c92-9fb9-856af227e652

2026-07-31T19:14:59.953491012Z   2026-07-31T19:14:59.953434Z  INFO [task_id=1195] segment_rewrite::segment_merger: CLUSTER 4 presence_mask=0b11100000 hasher_fields_bitset=0b11100000
    at libs/segment_rewrite/src/segment_merger.rs:4146
    in segment_rewrite::segment_merger::merge_planning with merge_id=019fb999-eaee-7c92-9fb9-856af227e652

2026-07-31T19:14:59.953566317Z   2026-07-31T19:14:59.953484Z  INFO [task_id=1195] segment_rewrite::segment_merger: Got initial plan of 97 plan items
    at libs/segment_rewrite/src/segment_merger.rs:3653
    in segment_rewrite::segment_merger::merge_planning with merge_id=019fb999-eaee-7c92-9fb9-856af227e652
```

Active merge planning, literally the second before the restart. There is no silence
anywhere in this window.

**Why the original diagnosis was wrong:** `docker logs --since <relative-time>`
(e.g. `--since 5m`, `--since 90s`) was returning false "0 lines" results on this host
— almost certainly because the underlying json-file log had grown very large (929 MB
for nursery's full history by the time this was checked) and relative-time queries
need to scan from the start of the file; short command timeouts were killing that
scan before it completed, producing an empty result that looked identical to genuine
silence. Bounded `--since X --until Y` queries with generous timeouts, or scanning
the raw file directly, gave the correct (non-empty) answer.

## Janitor: `download_config.max_concurrent_downloads` — YAML broken, CLI flag works

**Before (YAML, confirmed broken):** file at `/config/config.yaml` inside the running
container correctly contained:
```yaml
download_config:
  max_concurrent_downloads: 2
```
but the startup effective-config dump reported the default:
```
INFO [task_id=sync] bzrk_config::service:     max_concurrent_downloads: 20, service: "janitor"
```
(This exact contradiction — correct file content, wrong loaded value — was
established on 2026-07-27; see `docs/janitor-config-schema-2026-07-27.md`.)

**After (CLI flag, confirmed working, 2026-07-31T20:22:44Z):** `command:` changed to
`["-c", "/config/config.yaml", "--max-concurrent-downloads", "2"]`. Fresh startup log:
```
2026-07-31T20:22:44.209429Z  INFO [task_id=sync] bzrk_config::service:     max_concurrent_downloads: 2, service: "janitor"
```

Same value, same binary, same config file still mounted — only the delivery
mechanism (CLI flag vs. nested YAML field) differs in outcome. This isolates the bug
to the YAML deserialization path for that one nested field.

## Janitor: `max_segments_per_batch` does not cap merge job size — confirmed twice

**First occurrence (2026-07-31T20:10:44Z, before the CLI-flag fix):**
```
in segment_rewrite::workflow::merge_task with task_id=019fb9ba-e808-7071-9da1-980a0426f00a table_id=019e92d1-4e70-79f2-81eb-39e6d20022e1 segment_count=1000
```

**Second occurrence (after 20:22:44Z, with all three fixes — including the now-working
`max_concurrent_downloads: 2` — active):**
```
in segment_rewrite::workflow::merge_task with task_id=019fb9d7-7857-70e2-9877-2d25569e44da table_id=019e92d1-4e70-79f2-81eb-39e6d20022e1 segment_count=1000
```

Both merges ran with `max_segments_per_batch: 100` correctly loaded (confirmed via
the same startup dump mechanism). `segment_count=1000` in both cases — this is
consistent, repeatable behavior, not a one-off leftover from before any fix.

**Encouraging secondary finding:** during the second occurrence, host load and iowait
were dramatically lower than during the first, despite an identical `segment_count:
1000` merge running:

| | Load avg (1-min) | iowait |
|---|---|---|
| First occurrence (`max_concurrent_downloads` still broken, effectively 20) | 22.5–36.8 | 79–83% |
| Second occurrence (`max_concurrent_downloads: 2` confirmed active) | 4.09 | 6–7% |

This suggests the `max_concurrent_downloads` fix is doing real, meaningful work —
even though it doesn't reduce total segment count per job, capping concurrent
downloads from 20 to 2 appears to substantially reduce the instantaneous I/O pressure
one merge generates. Not a substitute for an actual job-size cap if one exists, but a
real mitigation on its own.
