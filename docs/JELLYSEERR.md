# Jellyseerr configuration

## Servers to add

### Arabic Radarr (Akwarr)

| Field          | Value                             |
| -------------- | --------------------------------- |
| Name           | Radarr (Arabic)                   |
| Hostname       | `akwarr-radarr` or `192.168.1.25` |
| Port           | `7879`                            |
| API Key        | Your `AKWARR_API_KEY`             |
| Sync enabled   | Yes                               |
| Default server | No                                |

Root folder shown by API: `/media/Movie/Arabic`

### Arabic Sonarr (Akwarr)

| Field          | Value                 |
| -------------- | --------------------- |
| Name           | Sonarr (Arabic)       |
| Hostname       | `akwarr-sonarr`       |
| Port           | `8990`                |
| API Key        | Your `AKWARR_API_KEY` |
| Sync enabled   | Yes                   |
| Default server | No                    |

Root folder: `/media/Serries/Arabic`

## User routing

**Advanced Requests** (recommended): grant permission `Advanced Request` so users pick Arabic servers per request.

**Override rules** (optional automation):

```
IF original language equals Arabic (ar)
THEN use Radarr (Arabic) + Sonarr (Arabic)
```

## Testing connectivity

From the Jellyseerr container:

```bash
curl -H "X-Api-Key: YOUR_KEY" http://akwarr-radarr:7879/api/v3/system/status
curl -H "X-Api-Key: YOUR_KEY" http://akwarr-sonarr:8990/api/v3/system/status
```

## Download ETA

Jellyseerr webhooks send notifications out of Jellyseerr; they are not an incoming progress channel. Akwarr reports download ETA through the Radarr/Sonarr queue API that Jellyseerr already polls:

```bash
curl -H "X-Api-Key: YOUR_KEY" http://akwarr-radarr:7879/api/v3/queue
curl -H "X-Api-Key: YOUR_KEY" http://akwarr-sonarr:8990/api/v3/queue?includeEpisode=true
```

When aria2 is actively downloading, queue records include `status=downloading`, `size`, `sizeleft`, `timeleft` in `HH:MM:SS`, `estimatedCompletionTime` as a future ISO timestamp, `downloadClient=aria2`, and the relevant movie or series/episode IDs. Jellyseerr displays remaining time from `estimatedCompletionTime`.

## Known limitation

Jellyseerr discovery is TMDB-only. Titles without a TMDB entry cannot be requested through Jellyseerr until they exist on TMDB.

Akwarr now bridges that TMDB request to Akwam by searching ElCinema for Arabic movie/series titles before Akwam lookup. Use `/ui` → ElCinema Bridge to confirm the Arabic candidate when an English Jellyseerr title does not find the right Akwam item.
