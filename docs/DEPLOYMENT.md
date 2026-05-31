# Deployment (Proxmox homelab)

> **Continuing in Codex?** Start with [CODEX-HANDOFF.md](./CODEX-HANDOFF.md) — full context, verification, and prompt template.

## Your layout

| LXC   | IP           | Role                                                       |
| ----- | ------------ | ---------------------------------------------------------- |
| CT107 | 192.168.1.25 | Docker media stack (Jellyseerr, Radarr, Sonarr, Portainer) |
| CT113 | 192.168.1.20 | Jellyfin                                                   |

Shared ZFS: `/mnt/pve/tv/Media`

- CT107 mount: `/media`
- CT113 mount: `/cc`

## 1. Create Arabic folders on CT107

```bash
ssh media
sudo mkdir -p /media/Movie/Arabic /media/Serries/Arabic /media/Download/akwarr-staging
sudo chown -R 1000:1000 /media/Movie/Arabic /media/Serries/Arabic /media/Download/akwarr-staging
sudo chmod -R 775 /media/Movie/Arabic /media/Serries/Arabic /media/Download/akwarr-staging
```

## 2. Deploy Akwarr

From the Mac, export the required keys and run the local deploy script. It syncs
this checkout to CT107 at `/opt/akwarr`, writes the remote `.env`, starts the
stack, configures Jellyseerr/Jellyfin, and runs `scripts/test-homelab.sh`.

```bash
export AKWARR_API_KEY=...
export TMDB_API_KEY=...
export JELLYFIN_API_KEY=...
export JELLYSEERR_API_KEY=...
SSH_TARGET=media scripts/run-deploy-local.sh
```

CT107 needs `rsync`, Docker, and either `docker compose` or legacy
`docker-compose`. If the existing media stack network is named
`media-stack_media_network`, setup auto-selects it; otherwise it uses
`media_network` or `MEDIA_NETWORK_NAME` when set.

Manual deploy on CT107 is also supported:

```bash
git clone https://github.com/Hassan220022/akwarr.git
cd akwarr
cp .env.example .env
# set keys, point MEDIA_ROOT=/media in compose override
docker compose -f docker-compose.yml -f docker-compose.media-stack.yml up -d --build
```

Ensure `akwarr-radarr` and `akwarr-sonarr` join the existing media stack network
that contains `jellyseerr` and `flaresolverr`.

## 3. Jellyfin libraries (CT113)

Add libraries pointing to:

- `/cc/Movie/Arabic`
- `/cc/Serries/Arabic`

Set metadata language Arabic; enable save artwork/metadata to media folders.

## 4. Jellyseerr

Follow [JELLYSEERR.md](./JELLYSEERR.md).

## 5. End-to-end test

Automated smoke test:

```bash
SSH_TARGET=media scripts/test-homelab.sh
```

Expected result: all checks pass.

1. Request an Arabic movie via Jellyseerr → Arabic Radarr
2. Watch logs: `docker logs -f akwarr-radarr`
3. Confirm file under `/media/Movie/Arabic/...`
4. Confirm Jellyfin shows the title with poster
5. Jellyseerr status → **Available**

## Permissions

Akwarr container runs as UID 1000. Match Jellyfin's read access:

```bash
# on CT113 after first import
ls -la /cc/Movie/Arabic/
```

If Jellyfin cannot read files, align `PUID`/`PGID` on aria2 and Akwarr with Jellyfin's user.
