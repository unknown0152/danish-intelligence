# About Danish Intelligence

Danish Intelligence is a single Cosmos-ready service for Danish media automation.

It replaces the previous split-container setup for Danish release proxying, OldBoys
translation, and DanskArr autopilot with one container that can self-configure a
local Prowlarr/Radarr/Sonarr stack.

The main goal is a one-click Cosmos Market install:

- User enters Prowlarr and OldBoys credentials in the Cosmos form.
- The service starts on the shared media network.
- Within startup, it paints Danish Custom Formats and Quality Profiles.
- Radarr and Sonarr indexers are rewired to the unified proxy endpoint.

Recommended GitHub repository description:

```text
Unified Danish media-stack intelligence for Cosmos, Prowlarr, Radarr, and Sonarr.
```

Recommended topics:

```text
cosmos, radarr, sonarr, prowlarr, newznab, danish, media-server, docker
```
