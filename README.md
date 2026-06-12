# Danish Intelligence

Unified Danish media-stack core for Cosmos, Prowlarr, Radarr, Sonarr, and Seerr.

Danish Intelligence combines Danish release proxying, OldBoys translation, and DanskArr
autopilot behavior into one container. It is designed for Cosmos Cloud Market
installs where the container starts once, joins the media network, and
automatically configures the local Arr stack.

## What It Does

- Proxies Prowlarr Newznab requests through Danish release intelligence.
- Detects Danish audio and Danish subtitles from release titles, NFOs, and
  indexer metadata.
- Exposes OldBoys as an optional Newznab-compatible proxy endpoint.
- Runs DanskArr autopilot on a schedule.
- Paints Danish Custom Formats and Quality Profiles into Radarr and Sonarr.
- Rewires Radarr/Sonarr indexers and AltMount download clients to use
  `http://danish-intelligence:9699`.
- Deploys Seerr in the full stack for request management without storing private
  Jellyfin, Radarr, Sonarr, or Seerr API keys in the market manifest.
- Keeps the legacy `dksubs-proxy` hostname as a network alias during migration.

## Cosmos Install

Market source:

```text
https://raw.githubusercontent.com/unknown0152/danish-intelligence/master/cosmos-market.json
```

Image:

```text
ghcr.io/unknown0152/danish-intelligence:latest
```

The Cosmos installer fields are optional:

- `ProwlarrKey`: Prowlarr API key, only needed if the Prowlarr config folder is not mounted.
- `OldBoysToken`: OldBoys API token, only needed for the OldBoys proxy.
- `OldBoysRSS`: OldBoys RSS key / RID, only needed for the OldBoys proxy.

On startup, the service waits briefly for the Arrs, then configures Radarr and
Sonarr through their HTTP APIs. It discovers Arr API keys from read-only config
mounts and does not require Docker socket access.

## Expected Network

The container expects Docker DNS names for the media stack:

- `http://prowlarr:9696`
- `http://radarr:7878`
- `http://sonarr:8989`
- `http://seerr:5055` when the full stack is installed
- `http://danish-intelligence:9699`
- `http://altmount:8080` when AltMount is installed

In Cosmos this is handled by attaching the service to the shared `media-stack`
network.

## Auto-Configured Arr Objects

Each startup paint pass manages these Custom Formats:

- `Danish Audio`
- `Danish Subtitles`
- `TrueHD Atmos`
- `DTS-X`
- `TrueHD`
- `DTS-HD MA`
- `EAC3 Atmos`
- `EAC3`
- `DTS`
- `AAC`
- `DV`
- `HDR`
- `HDR10`
- `HDR10+`
- `HEVC`

It also creates or updates:

- `Danish Audio` profile: `minFormatScore=10000`, `cutoffFormatScore=0`, Danish Audio `10000`, Danish Subtitles `0`.
- `Danish Subtitles` profile: `minFormatScore=10000`, `cutoffFormatScore=0`, Danish Subtitles `10000`, Danish Audio `0`.

The proxy emits `.DanishAudio` and `.DanishSubs` markers. Legacy `.DKaudio` and
`.DKOK` markers are accepted only as compatibility aliases. Post-import
automation should preserve the matching `[Danish Audio]` or `[Danish Subtitles]`
marker in the imported filename when the inner NZB file name does not contain the
proxy marker.

## Code Map

- `tags.py`: single source of truth for Danish markers, Arr CF names, profile
  names, and legacy aliases.
- `auto_config.py`: Cosmos-safe painter for Arr naming, root folders, CFs,
  profiles, proxy indexers, Prowlarr sync hardening, AltMount clients, and
  marker-preserver webhooks.
- `marker_preserver.py`: Radarr/Sonarr webhook handler that copies proxy-level
  Danish markers into imported `/media` symlink filenames when inner NZB file
  names lack the proxy marker.
- `app.py`: Newznab proxy request handler and status/health endpoints.
- `hunt.py`: Danish release detection pipeline and import-learning endpoint.
- `classification.py`: title, NFO, ffprobe, and mismatch classification helpers.
- `cache.py`: SQLite cache, request learning, indexer scoring, and scene-group
  learning.
- `service.py`: container entrypoint that combines the proxy, OldBoys, autopilot,
  and auto-painter.

Prowlarr application sync is set to `addOnly` so Prowlarr does not overwrite the
proxy URLs in Radarr/Sonarr.

## Runtime Verification

Useful checks:

```bash
docker ps --filter name=danish-intelligence
docker logs danish-intelligence
```

Healthy startup logs should include:

```text
[Core] Auto-Config: SUCCESS.
```

If OldBoys credentials are provided, startup logs should also include:

```text
[Core] OldBoys components initialized
```

Health endpoint:

```bash
docker exec danish-intelligence python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9699/health').read().decode())"
```

Expected response:

```json
{"status": "ok", "service": "danish-intelligence"}
```

Setup status endpoint:

```bash
docker exec danish-intelligence python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9699/status.json').read().decode())"
```

## Troubleshooting

If Auto-Config does not run:

- Confirm `PROWLARR_API_KEY` is set inside the container, or that Prowlarr config
  is mounted read-only at `/arr-config/prowlarr`.
- Confirm the container can reach `prowlarr`, `radarr`, and `sonarr` by Docker DNS.
- Confirm Arr config paths are mounted read-only at:
  - `/arr-config/prowlarr`
  - `/arr-config/radarr`
  - `/arr-config/sonarr`

If `/ob/health` reports `disabled`, add `OldBoysToken` and `OldBoysRSS` in
Cosmos and recreate the container. The rest of the service can run without
OldBoys.

If OldBoys fails with `PROXY_API_KEY` missing, update to the latest image. The
service now persists a fallback key in `/config/proxy_api_key` when Cosmos does
not materialize `{Passwords.32}`.

For AltMount integration:

- AltMount should be reachable as `http://altmount:8080`.
- Its SAB-compatible API should be enabled at `/sabnzbd`.
- Health monitoring, repair-on-import, segment cache, and ARR queue cleanup
  should stay enabled. Without these, AltMount can detect a corrupt NZB at
  read/import time, but Radarr/Sonarr may remain stuck at
  `Downloaded - Waiting to Import` instead of getting a clean retry/cleanup
  path.
- Playback-safe defaults keep failure masking enabled, cap background imports
  while streams are active, and size the segment cache for high-bitrate Plex
  playback.
- Import Processing is kept Radarr/Sonarr focused: video extensions only,
  sample filtering enabled, release-name renaming enabled, failed items cleaned
  after 24 hours, import history retained for 30 days, and completed NZBs kept
  for repair/debug workflows.
- Danish Intelligence defaults `ALTMOUNT_URL` to
  `http://altmount:8080/sabnzbd`.
- Radarr/Sonarr download clients should point to Danish Intelligence:
  host `danish-intelligence`, port `9699`, URL base `/altmount`.
- If `ALTMOUNT_APIKEY` is not supplied, the `/altmount` shim adds local
  Radarr/Sonarr API credentials on the internal request to AltMount.

For Seerr integration:

- The full stack deploys Seerr as `http://seerr:5055`.
- Seerr keeps its private state in `/app/config`, backed by
  `{Context.ConfigRoot}/seerr`.
- Seerr's first-run Jellyfin, Radarr, and Sonarr API keys belong in Seerr's UI
  or private config volume, never in the market JSON.

## Security Notes

- The container does not need `/var/run/docker.sock`.
- Arr config mounts are read-only and used only to discover local API keys.
- Request logs redact `apikey`, token, password, and similar query values.
- Do not commit real `.env` files or API keys.
