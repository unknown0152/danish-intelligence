# Danish Intelligence

Unified Danish media-stack core for Cosmos, Prowlarr, Radarr, and Sonarr.

Danish Intelligence combines DKSubs proxying, OldBoys translation, and DanskArr
autopilot behavior into one container. It is designed for Cosmos Cloud Market
installs where the container starts once, joins the media network, and
automatically configures the local Arr stack.

## What It Does

- Proxies Prowlarr Newznab requests through Danish release intelligence.
- Detects Danish audio and Danish subtitles from release titles, NFOs, and
  indexer metadata.
- Exposes OldBoys as a Newznab-compatible proxy endpoint.
- Runs DanskArr autopilot on a schedule.
- Paints Danish Custom Formats and Quality Profiles into Radarr and Sonarr.
- Rewires Radarr/Sonarr indexers to use `http://danish-intelligence:9699`.

## Cosmos Install

Market source:

```text
https://raw.githubusercontent.com/unknown0152/danish-intelligence/master/cosmos-market.json
```

Image:

```text
ghcr.io/unknown0152/danish-intelligence:latest
```

The Cosmos installer asks for:

- `ProwlarrKey`: Prowlarr API key.
- `OldBoysToken`: OldBoys API token.
- `OldBoysRSS`: OldBoys RSS key / RID.

On startup, the service waits briefly for the Arrs, then configures Radarr and
Sonarr through their HTTP APIs. It discovers Arr API keys from read-only config
mounts and does not require Docker socket access.

## Expected Network

The container expects Docker DNS names for the media stack:

- `http://prowlarr:9696`
- `http://radarr:7878`
- `http://sonarr:8989`
- `http://danish-intelligence:9699`

In Cosmos this is handled by attaching the service to the shared `media-stack`
network.

## Auto-Configured Arr Objects

Each startup paint pass manages these Custom Formats:

- `DKAudio`
- `DKSubs`
- `TrueHD Atmos`
- `DTS-X`
- `TrueHD`
- `DTS-HD MA`
- `EAC3 Atmos`
- `EAC3`
- `DTS`
- `AAC`

It also creates or updates:

- `DanishAudio`
- `EnglishSubs`

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
[Core] OldBoys components initialized
[Core] Auto-Config: SUCCESS.
```

Health endpoint:

```bash
docker exec danish-intelligence python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9699/health').read().decode())"
```

Expected response:

```json
{"status": "ok", "service": "danish-intelligence"}
```

## Troubleshooting

If Auto-Config does not run:

- Confirm `PROWLARR_API_KEY` is set inside the container.
- Confirm the container can reach `prowlarr`, `radarr`, and `sonarr` by Docker DNS.
- Confirm Radarr and Sonarr config paths are mounted read-only at:
  - `/arr-config/radarr`
  - `/arr-config/sonarr`

If OldBoys fails with `PROXY_API_KEY` missing, update to the latest image. The
service now persists a fallback key in `/config/proxy_api_key` when Cosmos does
not materialize `{Passwords.32}`.

## Security Notes

- The container does not need `/var/run/docker.sock`.
- Arr config mounts are read-only and used only to discover local API keys.
- Request logs redact `apikey`, token, password, and similar query values.
- Do not commit real `.env` files or API keys.

