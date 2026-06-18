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
- Enables Arr renaming and writes IMDb/TMDb IDs, quality, and Custom Format
  markers into imported filenames.
- Whitelists the proxy's Danish subtitle markers in Arr indexer settings so the
  Arrs do not treat them as real hardcoded subtitle tags.
- Rewires Radarr/Sonarr indexers and AltMount download clients to use
  `http://danish-intelligence:9699`.
- Registers Radarr/Sonarr back into AltMount ARR Management for queue/file
  synchronization without Remote Path Mappings.
- Deploys Seerr in the full stack for request management without storing private
  Jellyfin, Radarr, Sonarr, or Seerr API keys in the market manifest.
- Recognizes `dksubs-proxy` as a compatibility hostname for older saved Arr
  indexer URLs. The standalone core manifest also exposes that Docker DNS alias.

## Cosmos Install

Market source:

```text
https://raw.githubusercontent.com/unknown0152/danish-intelligence/master/cosmos-market.json
```

Image:

```text
ghcr.io/unknown0152/danish-intelligence:latest
```

Tagged releases also publish matching Docker image tags. The Cosmos market
points to tagged compose files, and those full-stack compose files pin Danish
Intelligence to the same release image tag so clean installs are reproducible.

Market entries:

- `Prowlarr (Danish Prerequisite)`: install first on clean servers, then add
  your indexers in Prowlarr.
- `Danish Media Stack (Jellyfin Edition)`: Jellyfin, Seerr, Radarr, Sonarr,
  AltMount, and Danish Intelligence.
- `Danish Media Stack (Plex Edition)`: Plex, Seerr, Radarr, Sonarr, AltMount,
  and Danish Intelligence.
- `Danish 2160p Arr Add-on`: optional dedicated `radarr-2160p` and
  `sonarr-2160p` containers for true separate standard/2160p copies.
- `Danish Intelligence`: standalone core for existing Arr/AltMount stacks.
- `AltMount (Danish Edition)`: standalone patched AltMount.

The core installer fields are optional:

- `ProwlarrKey`: Prowlarr API key. Danish Intelligence first tries mounted
  Prowlarr config, then this explicit field.
- `OldBoysToken`: OldBoys API token, only needed for the OldBoys proxy.
- `OldBoysRSS`: OldBoys RSS key / RID, only needed for the OldBoys proxy.

For clean servers, install `Prowlarr (Danish Prerequisite)` from this market
source first and configure its indexers there. That prerequisite uses the shared
`media-stack` Docker network and the lowercase `prowlarr` DNS alias expected by
the full stack. Installing generic Prowlarr from another Cosmos market can leave
Prowlarr isolated on its own `cosmos-Prowlarr-default` network.

On startup, the service waits briefly for the Arrs, then configures Radarr and
Sonarr through their HTTP APIs. It discovers Arr API keys from read-only config
mounts and does not require Docker socket access.

The market offers two full-stack editions:

- `Danish Media Stack (Plex Edition)` deploys Plex as the media server.
- `Danish Media Stack (Jellyfin Edition)` deploys Jellyfin as the media server.

Both editions include Seerr, Radarr, Sonarr, AltMount, and Danish Intelligence.
Only the selected media server is deployed. The base Plex/Jellyfin entries
install the standard Radarr/Sonarr pair.

For true separate standard and 2160p copies, install `Danish 2160p Arr Add-on`
after the base stack, using the same `ConfigRoot`, `MediaRoot`, `PUID`, and
`PGID`. Then restart `danish-intelligence`. The base stack already mounts the
future 2160p config paths read-only, so Danish Intelligence auto-detects
`radarr-2160p` and `sonarr-2160p` when their `config.xml` files appear. It then
registers the 2160p Arrs in Prowlarr, paints separate 2160p root folders, adds
matching Seerr server entries, creates strict `Danish Audio 2160p` and
`Danish Subtitles 2160p` profiles, uses separate AltMount SAB categories
(`movies-2160p` and `tv-2160p`), and creates `Movies 2160p` / `TV Shows 2160p`
libraries in Plex or Jellyfin when the media-server API is available.

The full-stack editions create this media/library shape:

```text
/media/movies
/media/danish-movies
/media/documentaries
/media/tv
/media/danish-tv
/media/kids-movies
/media/kids-tv
/media/movies-2160p        optional add-on
/media/tv-2160p            optional add-on
```

Radarr uses `/media/movies`, `/media/danish-movies`, `/media/documentaries`,
and `/media/kids-movies`. Sonarr uses `/media/tv`, `/media/danish-tv`, and
`/media/kids-tv`. Jellyfin or Plex gets matching libraries, and Seerr receives
matching movie/TV root-folder choices. With the 2160p add-on, Radarr 2160p uses
`/media/movies-2160p`, Sonarr 2160p uses `/media/tv-2160p`, and the media
server gets matching `Movies 2160p` and `TV Shows 2160p` libraries.

## Expected Network

The container expects Docker DNS names for the media stack:

- `http://prowlarr:9696`
- `http://radarr:7878`
- `http://sonarr:8989`
- `http://radarr-2160p:7878` when the optional 2160p Arrs are enabled
- `http://sonarr-2160p:8989` when the optional 2160p Arrs are enabled
- `http://seerr:5055` when the full stack is installed
- `http://plex:32400` in the Plex edition
- `http://jellyfin:8096` in the Jellyfin edition
- `http://danish-intelligence:9699`
- `http://altmount:8080` when AltMount is installed

In Cosmos this is handled by the market manifests creating the shared
`media-stack` bridge network and attaching every stack service to it. The
network uses Docker's normal IPAM allocation instead of a fixed subnet, so fresh
servers do not need a pre-created network and avoid subnet collisions with
existing Docker networks.

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
- `Danish Audio 2160p` profile on optional 2160p Arrs: same Danish Audio requirement, with non-2160p qualities disabled.
- `Danish Subtitles 2160p` profile on optional 2160p Arrs: same Danish Subtitles requirement, with non-2160p qualities disabled.

Normal Danish profiles allow DVD/DVD-R and 720p as fallback qualities, plus
1080p/2160p, so older Danish titles can import when no HD release exists and
still upgrade later. Unsafe or ambiguous qualities such as Unknown, CAM, TS,
DVDSCR, SDTV, and Raw-HD remain disabled. The dedicated `2160p` profiles stay
strictly 2160p-only.

The proxy emits `.DanishAudio` and `.DanishSubs` markers. Legacy `.DKaudio` and
`.DKOK` markers are accepted only as compatibility aliases. Post-import
automation should preserve the matching `[Danish Audio]` or `[Danish Subtitles]`
marker in the imported filename when the inner NZB file name does not contain the
proxy marker.

Native Danish movies and shows that do not advertise `DANiSH`/`DANSK` in the
release title can be trusted through `/config/native-dk-titles.txt`, one title
per line. Matching is separator-tolerant, so `Villads fra Valby` matches scene
titles like `Villads.Fra.Valby.2015.1080p.WEB...`.

For Radarr movie searches, the proxy also uses the incoming `tmdbid`/`imdbid`
context to ask Radarr whether the exact movie's original language is Danish. If
so, Radarr/TMDb titles from that movie are used as temporary native-title
matches for that search. Danish letters are matched against common scene ASCII
folds, so `Fræk` also matches `Fraek`.

For Radarr text searches such as `Dreng 2011`, the proxy can also match the
query back to the local Radarr movie list. This only activates when the query
contains a year and exactly one Danish-original Radarr movie has that exact
title or original title, keeping short generic titles like `Boy` from becoming
global false positives.

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

Prowlarr application sync is temporarily switched to `fullSync` during the
initial paint pass when indexers need to be pushed into the Arrs, then returned
to `addOnly` so Prowlarr does not overwrite the proxy URLs in Radarr/Sonarr.
Danish Intelligence also trims unnecessary Prowlarr sync categories from the app
links so category noise does not get pushed into the Arrs.

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

Install diagnostics are enabled by default in the Cosmos manifests. They are
redacted and persist in the Danish Intelligence config volume:

```text
/config/install-debug.jsonl
/config/install-debug-latest.json
```

Inside the running container, the same recent events are available at:

```text
http://danish-intelligence:9699/debug/install
```

The diagnostics capture environment placeholder state, mounted config/media
paths, Docker DNS resolution, API reachability, Prowlarr app discovery, and each
auto-config paint stage. API keys, tokens, passwords, and RID values are
redacted.

For a full SSH-side server report, run this on the target server:

Fast live debug output:

```bash
curl -fsSL https://raw.githubusercontent.com/unknown0152/danish-intelligence/master/tools/live-server-debug.sh -o /tmp/live-server-debug.sh && bash /tmp/live-server-debug.sh
```

Full redacted archive:

```bash
curl -fsSL https://raw.githubusercontent.com/unknown0152/danish-intelligence/master/tools/collect-server-debug.sh -o /tmp/collect-server-debug.sh && bash /tmp/collect-server-debug.sh
```

Both scripts are read-only except for writing terminal output or reports under
`/tmp`. The full collector packages system, Docker, Cosmos, network, filesystem,
container log, and Danish Intelligence debug data into one redacted `.tar.gz`
archive.

## Troubleshooting

If Auto-Config does not run:

- Confirm `PROWLARR_API_KEY` is set inside the container, or that Prowlarr config
  is mounted read-only at `/arr-config/prowlarr`.
- Confirm the container can reach `prowlarr`, `radarr`, and `sonarr` by Docker DNS.
- Confirm Arr config paths are mounted read-only at:
  - `/arr-config/prowlarr`
  - `/arr-config/radarr`
  - `/arr-config/sonarr`

If the 2160p add-on is installed but not painted:

- Confirm `radarr-2160p` and `sonarr-2160p` are running on the `media-stack`
  network.
- Confirm Danish Intelligence can see `/arr-config/radarr-2160p/config.xml`
  and `/arr-config/sonarr-2160p/config.xml`.
- Restart `danish-intelligence` to re-run the idempotent auto-painter. The base
  stack uses `ENABLE_2160P_ARRS=auto`, so no manual environment edit is needed.

If `/ob/health` reports `disabled`, add `OldBoysToken` and `OldBoysRSS` in
Cosmos and recreate the container. The rest of the service can run without
OldBoys.

If OldBoys or the AltMount shim fails with `PROXY_API_KEY` missing, update to
the latest release image. The service persists a fallback key in
`/config/proxy_api_key` when Cosmos does not materialize `{Passwords.32}`.

For AltMount integration:

- AltMount should be reachable as `http://altmount:8080`.
- Its SAB-compatible API should be enabled at `/sabnzbd`.
- Health monitoring, segment cache, and ARR queue cleanup should stay enabled.
  Automatic repair and repair-on-import stay disabled by default so AltMount can
  report bad files without deleting, blocklisting, or replacing media through
  Radarr/Sonarr.
- Playback-safe defaults disable streaming failure masking so real segment/NZB
  failures are visible, cap background imports while streams are active, and
  size the segment cache for high-bitrate playback.
- The full stack pins the native FUSE mount shape used by the Arrs:
  mount type `fuse`, mount path `/mnt/altmount`, and metadata under
  `/config/metadata`.
- Import Processing is kept Radarr/Sonarr focused: video extensions only,
  sample filtering enabled, release-name renaming enabled, failed items cleaned
  after 24 hours, import history retained for 30 days, and completed NZBs kept
  for repair/debug workflows.
- ARR queue cleanup is enabled with a 10 minute grace period. Automatic orphan
  metadata cleanup, automatic repair, and repair-on-import remain disabled by
  default.
- The full stack defines AltMount SAB categories from the manifest. Standard
  Arrs use `movies` and `tv`; the optional 2160p add-on uses `movies-2160p`
  and `tv-2160p`.
- Danish Intelligence defaults `ALTMOUNT_URL` to
  `http://altmount:8080/sabnzbd`.
- Radarr/Sonarr download clients should point to Danish Intelligence:
  host `danish-intelligence`, port `9699`, URL base `/altmount`.
- The Docker-internal Danish Intelligence to AltMount API key is fixed in the
  market manifest so Cosmos cannot expand two random placeholders into
  mismatched keys. AltMount requires this override to be exactly 32 characters.
  The public Arr-facing `PROXY_API_KEY` remains random per install.
- The `/altmount` shim translates Arr SAB requests to AltMount's internal API
  using the private AltMount key, so Radarr and Sonarr only need the generated
  Arr-facing `PROXY_API_KEY`.

For Seerr integration:

- The full stack deploys Seerr as `http://seerr:5055`.
- Seerr keeps its private state in `/app/config`, backed by
  `{Context.ConfigRoot}/seerr`.
- A short-lived `seerr-bootstrap` service seeds Seerr's private `settings.json`
  before Seerr starts, so Seerr is not stuck in first-run mode on clean installs.
- Danish Intelligence mounts the Seerr config at `/seerr-config`, creates the
  first local Seerr admin only when the Seerr database has no users, then adds
  the discovered Radarr/Sonarr entries through Seerr's API.
- The generated local Seerr admin is for bootstrap and recovery. Normal users
  can still sign in with Jellyfin or Plex when that media server is configured.
- The install form exposes `SeerrAdminEmail` and optional
  `SeerrAdminPassword`. If the password is left blank, Danish Intelligence
  generates one and saves it at
  `{Context.ConfigRoot}/danish-intelligence/seerr-admin-password.txt`.
- Jellyfin/Plex details come from optional installer fields or the private
  config volume. Private API keys are never stored in the market JSON.

For Jellyfin integration:

- The Jellyfin edition mounts Jellyfin's private config into Danish Intelligence
  at `/jellyfin-config` so first boot can create or reuse a Jellyfin API key
  named `Danish Intelligence`.
- Danish Intelligence creates the clean Jellyfin libraries automatically:
  `Movies`, `Danish Movies`, `Documentaries`, `TV Shows`, `Danish TV`,
  `Kids Movies`, and `Kids TV`.
- When the 2160p add-on is detected, it also creates `Movies 2160p` and
  `TV Shows 2160p`.
- Seerr receives the same private Jellyfin API key through Seerr's API, so
  Jellyfin login and availability checks work without storing a Jellyfin key in
  the public market manifest.

For Plex integration:

- The Plex edition mounts Plex's private config into Danish Intelligence at
  `/plex-config` so first boot can use Plex's local admin token when Plex has
  generated one.
- Danish Intelligence creates the clean Plex libraries automatically:
  `Movies`, `Danish Movies`, `Documentaries`, `TV Shows`, `Danish TV`,
  `Kids Movies`, and `Kids TV`.
- When the 2160p add-on is detected, it also creates `Movies 2160p` and
  `TV Shows 2160p`.
- The Plex install form exposes optional `PlexClaim` and `PlexToken` fields.
  `PlexClaim` is passed only to the Plex container so users can claim the server
  through Plex's normal flow. `PlexToken` can be used when an existing Plex
  access token should be passed to Seerr immediately.
- If neither token is supplied, Danish Intelligence falls back to Plex's local
  `.LocalAdminToken` for local library creation and local Seerr server checks.
  This does not create a Plex cloud account login by itself.

## Permissions

- The full-stack install form exposes `PUID` and `PGID`. Use the UID/GID that
  owns your media and app config folders. The default is `1001:1001`, matching
  this reference server.
- Recommended host layout:
  `/srv/config` and `/srv/media` owned by `media:media`, group-writable, setgid,
  and backed by default ACLs for the container UIDs/GIDs used on the server.
  The Golden Build may grant write ACLs to both `1000` and `1001`; set the
  install form `PUID`/`PGID` to the UID/GID you want the LinuxServer containers
  to use.
- Keep `/srv/docker` root-managed and not generally writable. The media stack
  only needs bind-mount access to config/media paths, not write access to the
  Docker data-root.
- `/mnt` must exist on the host and allow AltMount to create `/mnt/altmount`.
  The stack mounts `/mnt` into AltMount, Radarr, Sonarr, and the media server
  with `rshared` propagation so the FUSE mount is visible everywhere.
- AltMount requires `/dev/fuse`, `SYS_ADMIN`, and
  `security_opt=["apparmor=unconfined"]` for the native FUSE mount.

## Security Notes

- The container does not need `/var/run/docker.sock`.
- Arr config mounts are read-only and used only to discover local API keys.
- Request logs redact `apikey`, token, password, and similar query values.
- Do not commit real `.env` files or API keys.
