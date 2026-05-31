# Architecture

## Modules

| Path                             | Purpose                                       |
| -------------------------------- | --------------------------------------------- |
| `akwarr/api/radarr.py`           | Radarr v3 API for Jellyseerr (movies)         |
| `akwarr/api/sonarr.py`           | Sonarr v3 API for Jellyseerr (series)         |
| `akwarr/scraper/akwam.py`        | Search, metadata, download link resolution    |
| `akwarr/scraper/elcinema.py`     | Arabic title candidates from ElCinema works   |
| `akwarr/scraper/flaresolverr.py` | Cloudflare-aware HTTP fetch                   |
| `akwarr/download/aria2.py`       | aria2 JSON-RPC client                         |
| `akwarr/library/organizer.py`    | Folder naming, move, permissions              |
| `akwarr/library/metadata.py`     | NFO + poster/fanart sidecars                  |
| `akwarr/library/jellyfin.py`     | Post-import library refresh                   |
| `akwarr/core/store.py`           | SQLite state (movies, series, episodes, jobs) |
| `akwarr/core/worker.py`          | Background download + import loop             |
| `akwarr/core/tmdb.py`            | TMDB lookup for Jellyseerr compatibility      |

## Request flow (movie)

1. User requests a TMDB title in Jellyseerr → Arabic Radarr server
2. Jellyseerr `POST /api/v3/movie` → Akwarr
3. Akwarr loads TMDB metadata, asks ElCinema for Arabic work-title candidates, searches Akwam, stores record + queues job
4. Worker resolves direct URL → aria2 download → staging
5. Organizer moves file, writes `movie.nfo`, downloads artwork
6. Jellyfin `Library/Media/Updated` for the folder
7. Jellyseerr sync sees `hasFile: true`

## Data stores

- **SQLite** (`DATA_PATH/akwarr.db`) — authoritative for Jellyseerr availability
- **Filesystem** — final media + sidecar metadata for Jellyfin
- **aria2** — transient download queue

## Dual-container model

Radarr and Sonarr shims run as separate processes (ports 7879 / 8990) with separate SQLite databases. They share:

- `/media` volume; movies import to `/media/Movie/Arabic`, series import to `/media/Serries/Arabic`, and downloads stage in `/media/Download/akwarr-staging`
- FlareSolverr + aria2 services
- Same Docker image, different `command` and `MODE`
