# Windows Forensics Primer — Berserk MCP

This lane is schema-gated. The latest recorded Berserk inventory contains infrastructure,
Claude Code, and MCP-server telemetry, but no confirmed Windows Security, Sysmon, or Windows
PowerShell event source. Therefore no fixed `win_*` tools ship yet: querying guessed fields
would create false confidence during an investigation.

## What to ship

Send these channels through an OpenTelemetry Collector `windowseventlog` receiver (or an
equivalent Windows Event Forwarding agent that exports OTLP) with a stable `service.name`:

- **Windows Security**: 4624, 4625, and 4648 logons; 4688 process creation; 4698 scheduled tasks
- **Windows System**: 7045 service installation
- **Microsoft-Windows-PowerShell/Operational**: 4104 script-block logging
- **Microsoft-Windows-Sysmon/Operational**: event 1 process creation and event 13 registry changes

Preserve the event ID, provider/channel, computer, account/SID, source address, logon type,
parent and child process names, command line, registry target, task/service name, and original
body. Treat command lines and script blocks as secret-bearing data and keep MCP output redaction
enabled.

## Activation procedure

1. Call `list_services` and identify the stable Windows event `service.name`.
2. Call `discover_schema service=<name>` with a bounded window and record the real nested keys.
3. Confirm whether the event fields are flattened attributes or JSON embedded in `body`.
4. Build each `win_*` query against those observed fields, verify it live, and only then add it as
   a role-tagged fixed tool. Save one-off verified queries with `roles=windows-forensics`.

Until those checks succeed, use `suggest_ingestion role_or_usecase=soc/endpoint-identity` for
ingestion guidance. Do not hand-author Windows KQL against customary `winlog.*` or Sysmon field
names and present it as verified.
