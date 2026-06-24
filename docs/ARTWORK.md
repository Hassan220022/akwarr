# Artwork pipeline

Akwarr writes Jellyfin-compatible sidecar artwork on every import and re-validates it on a schedule.

## Files per title

| Kind   | Required files                           | Legacy aliases (audit copies to standard) |
| ------ | ---------------------------------------- | ----------------------------------------- |
| Series | `poster.jpg`, `fanart.jpg`, `folder.jpg` | `folder.jpg` only                         |
| Movie  | `poster.jpg`, `fanart.jpg`               | `folder.jpg`, `landscape.jpg`             |

Episode thumbs (`*-thumb.jpg`) are **never** promoted to series posters.

## Resolution order

When `tmdb_id` is set and `TMDB_API_KEY` is configured:

1. **TMDB** poster + backdrop
2. Stored `poster_url` / `fanart_url` in the akwarr DB (if valid)
3. **Akwam** scrape (only for missing slots)

Without TMDB, steps 2–3 still run.

## Hard guards

Rejected at URL, download, and disk:

- SVG / XML / site logos (`logo-white.svg`, `/style/assets/`, etc.)
- Images smaller than **50 KB**
- `poster.jpg` whose MD5 matches any `*-thumb.jpg` in the series folder

Invalid DB `poster_url` / `fanart_url` values are cleared on store startup.

## When artwork is refreshed

- **Every successful import** — `refresh_*_artwork()` + NFO `<art>` patch + Jellyfin path refresh
- **Weekly worker task** — monitored titles with invalid/missing `poster.jpg` (configurable interval)
- **Manual audit** — `scripts/arabic-media-audit.py --fix`

## Required environment variables

| Variable                            | Required                 | Purpose                                                  |
| ----------------------------------- | ------------------------ | -------------------------------------------------------- |
| `TMDB_API_KEY`                      | **Strongly recommended** | TMDB poster/backdrop fallback when Akwam URLs are bad    |
| `JELLYFIN_API_KEY`                  | Recommended              | Trigger library/path refresh after artwork fixes         |
| `JELLYFIN_URL`                      | Recommended              | Jellyfin base URL (default `http://192.168.1.20:8096`)   |
| `SAVE_AKWAM_ARTWORK`                | Optional                 | `true` (default) — download and maintain sidecar art     |
| `ARTWORK_VALIDATE_INTERVAL_SECONDS` | Optional                 | Periodic validation interval (default `604800` = 7 days) |

Radarr/Sonarr/Jellyseerr on CT107 often already have a TMDB key — copy the same value into `/opt/akwarr/.env`.

## Audit and fix

On CT107 (host Python may lack deps — use the akwarr image):

```bash
ssh media 'docker run --rm -v /opt/akwarr:/opt/akwarr -v /media:/media --env-file /opt/akwarr/.env -e PYTHONPATH=/opt/akwarr -w /opt/akwarr akwarr_akwarr-sonarr python3 scripts/arabic-media-audit.py --fix'
```

Or on the host after `pip install httpx` and with `PYTHONPATH=/opt/akwarr`:

```bash
cd /opt/akwarr && set -a && source .env && set +a && python3 scripts/arabic-media-audit.py --fix
```

JSON summary is written to `/tmp/arabic-audit.json`.
