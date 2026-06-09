# Akwarr

**Akwarr** is a Radarr/Sonarr API shim for [Jellyseerr](https://github.com/Fallenbagel/jellyseerr). It downloads Arabic movies and series from [Akwam](https://akwam.it), organizes files for [Jellyfin](https://jellyfin.org), and reports availability back to Jellyseerr — without touching your English Radarr/Sonarr/Deluge stack.

```
Jellyseerr  →  Akwarr (Radarr/Sonarr API)  →  Akwam + FlareSolverr + aria2
                    ↑
              ElCinema Arabic title bridge
                    ↓
              /media/Movie/Arabic and /media/Serries/Arabic
                    ↓
              Jellyfin (shared ZFS library)
```

## Features

- **Jellyseerr-native requests** — add Akwarr as extra Radarr/Sonarr servers; users pick Arabic in Advanced Requests
- **TMDB + IMDb + ElCinema metadata** — Jellyfin NFO sidecars include TMDB, IMDb, and ElCinema IDs when available
- **TVDB-to-TMDB fallback** — Sonarr-style Jellyseerr TV requests can resolve through TheTVDB's public TMDB link when TMDB's external-ID lookup misses
- **Arabic-first Akwam matching** — converts Jellyseerr/TMDB English titles into Arabic search candidates with ElCinema, then searches Akwam with Arabic candidates only when any Arabic title is available
- **Akwam artwork fallback** — saves `poster.jpg` / `fanart.jpg` from Akwam when useful
- **Jellyfin-friendly layout** — standard movie folders and `Season XX/SxxExx` episode naming
- **HTTP downloads via aria2** — Akwam direct links (not torrents; Deluge stays English-only)
- **Cloudflare bypass** — FlareSolverr integration with session reuse

## Quick start (Docker)

```bash
git clone https://github.com/Hassan220022/akwarr.git
cd akwarr
cp .env.example .env
# Edit .env: AKWARR_API_KEY, TMDB_API_KEY, JELLYFIN_API_KEY

docker compose up -d --build
```

| Service               | Port       | Role              |
| --------------------- | ---------- | ----------------- |
| `akwarr-radarr`       | 7879       | Movies API shim   |
| `akwarr-sonarr`       | 8990       | Series API shim   |
| `akwarr-flaresolverr` | 8192       | Cloudflare bypass |
| `akwarr-aria2`        | 6800 (RPC) | Download queue    |

## Storage layout

On CT107 (Docker): `/media/Movie/Arabic` and `/media/Serries/Arabic`
On CT113 (Jellyfin): `/cc/Movie/Arabic` and `/cc/Serries/Arabic` (same ZFS pool)

```
/media/Movie/Arabic/
└── الست (2026)/
    ├── الست (2026) 720p.mkv
    ├── movie.nfo
    ├── poster.jpg
    └── fanart.jpg

/media/Serries/Arabic/
└── اللعبة (2025)/
    ├── tvshow.nfo
    ├── poster.jpg
    └── Season 01/
        └── اللعبة - S01E01 720p.mkv

/media/Download/akwarr-staging/   # aria2 incomplete downloads
```

## Jellyseerr setup

1. **Settings → Services → Radarr** — add server:
   - Host: `http://akwarr-radarr:7879` (or your LAN IP)
   - API Key: same as `AKWARR_API_KEY`
   - Default: **No**

2. **Settings → Services → Sonarr** — add server:
   - Host: `http://akwarr-sonarr:8990`
   - API Key: same as `AKWARR_API_KEY`
   - Default: **No**

3. Enable **Advanced Requests** so users can pick Arabic servers in the request modal.

4. Optional **Override Rule**: if `original_language` is `ar` → route to Arabic Radarr/Sonarr.

### Jellyseerr failed-series lookup notes

Jellyseerr may send Sonarr requests by TVDB ID, for example `tvdb:477454` for `Ward Ala Foll Wa Yasmeen (2026)`. If TMDB's external-ID lookup has no TV result for that TVDB ID, Akwarr falls back to TheTVDB's public series page and follows the linked TMDB series before returning the Sonarr-compatible lookup payload. The known mapping for this case is:

| Jellyseerr / TVDB | TMDB | Akwam |
| ----------------- | ---- | ----- |
| `tvdb:477454` / `Ward Ala Foll Wa Yasmeen (2026)` | `tmdb:299988` / `ورد على فل وياسمين` | `https://akwam.it/series/5658/ورد-على-فل-وياسمين` |

Live probe from CT107:

```bash
ssh media 'set -a; . /opt/akwarr/.env; set +a; curl -G -H "X-Api-Key: $AKWARR_API_KEY" --data-urlencode "term=tvdb:477454" http://127.0.0.1:8990/api/v3/series/lookup'
```

## Admin monitor

Akwarr exposes an API-key protected monitor on both shim containers:

- Radarr mode: `http://<host>:7879/ui?apikey=<AKWARR_API_KEY>`
- Sonarr mode: `http://<host>:8990/ui?apikey=<AKWARR_API_KEY>`
- LAN-only homelab URL: `https://akwam.mikawi.org/ui`

The monitor shows ElCinema Arabic title candidates, Akwam search results, metadata/download links, recent jobs, failed errors, active download percent/speed/ETA, and imported files under `/media/Movie/Arabic` and `/media/Serries/Arabic`. After loading exact Akwam series metadata, the monitor can queue all episodes or only checked episodes directly into the download jobs table. Active downloads can be paused, resumed, or deleted from the jobs table.

The Akwarr API is also available on the same LAN-only host without a query API key, for example `https://akwam.mikawi.org/api/v3/system/status`. The LAN proxy injects `X-Api-Key`; direct container access still requires the normal key.

## Jellyseerr download ETA

Jellyseerr's webhook notification system is outbound-only. Download progress is exposed through the normal Radarr/Sonarr queue contract instead:

- Movies: `GET /api/v3/queue` on the Akwarr Radarr shim.
- Series episodes: `GET /api/v3/queue` on the Akwarr Sonarr shim.

Active Akwarr jobs include `status`, `size`, `sizeleft`, `timeleft`, `estimatedCompletionTime`, `downloadClient=aria2`, and movie/series/episode identifiers so Jellyseerr can see how many minutes remain while aria2 is downloading.

## Jellyfin setup (CT113)

Create two libraries:

| Library       | Path                |
| ------------- | ------------------- |
| Arabic Movies | `/cc/Movie/Arabic` |
| Arabic Series | `/cc/Serries/Arabic` |

Recommended library settings:

- Metadata language: **Arabic**
- Local metadata/NFO reader: **On**
- Save artwork into media folders: **On**
- Save metadata into media folders: **On**

Akwarr writes `movie.nfo` and `tvshow.nfo` with Jellyfin-readable `uniqueid` entries for TMDB plus IMDb and ElCinema when those IDs are available. ElCinema is also used before Akwam search so English Jellyseerr/TMDB titles can auto-search Arabic movie and series titles.

## Homelab integration (CT107)

See [docs/CODEX-HANDOFF.md](docs/CODEX-HANDOFF.md) for full homelab context, deploy scripts, and verification when continuing in Codex.

If you already run FlareSolverr in your media stack:

```bash
docker compose -f docker-compose.yml -f docker-compose.media-stack.yml up -d
```

This reuses `flaresolverr:8191` and mounts `/media:/media` on the host.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check akwarr tests
```

Run locally:

```bash
MODE=radarr DATA_PATH=./data/radarr akwarr-radarr
MODE=sonarr DATA_PATH=./data/sonarr akwarr-sonarr
```

## Environment variables

See [`.env.example`](.env.example) for the full list. Key values:

| Variable                            | Description                          |
| ----------------------------------- | ------------------------------------ |
| `AKWARR_API_KEY`                    | Jellyseerr X-Api-Key                 |
| `TMDB_API_KEY`                      | TMDB/IMDb metadata for titles + NFO  |
| `ELCINEMA_ENABLE` / `ELCINEMA_BASE` | Arabic title bridge + NFO source URL |
| `ARIA2_SECRET`                      | aria2 JSON-RPC token, default `P3TERX` |
| `BANDWIDTH_TEST_URL` / `BANDWIDTH_TEST_SECONDS` | URL and max seconds for setup to measure current WAN download speed |
| `BANDWIDTH_MEASURED_MBIT` / `ARIA2_BANDWIDTH_LIMIT_PERCENT` | Measured WAN Mbps and percent cap; default cap percent is 60% |
| `ARIA2_MAX_OVERALL_DOWNLOAD_LIMIT`  | aria2 total download cap calculated from measured speed; set only for manual override |
| `JELLYFIN_URL` / `JELLYFIN_API_KEY` | Trigger library refresh after import |
| `MOVIES_PATH` / `SERIES_PATH`       | Final media destinations             |
| `STAGING_PATH`                      | Temporary aria2 download area        |

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for module map and request flow.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

For personal homelab use. Respect copyright laws in your jurisdiction. Akwarr automates access to third-party sources; you are responsible for how you use it.
