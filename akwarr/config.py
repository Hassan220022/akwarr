"""Application configuration from environment."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    mode: str = Field(default="radarr", alias="MODE")
    api_key: str = Field(default="change-me", alias="AKWARR_API_KEY")

    akwam_base: str = Field(default="https://akwam.it", alias="AKWAM_BASE")
    elcinema_base: str = Field(default="https://elcinema.com", alias="ELCINEMA_BASE")
    elcinema_enable: bool = Field(default=True, alias="ELCINEMA_ENABLE")
    flaresolverr_url: str = Field(
        default="http://flaresolverr:8191/v1", alias="FLARESOLVERR_URL"
    )
    flaresolverr_enable: bool = Field(default=True, alias="FLARESOLVERR_ENABLE")
    flaresolverr_auto: bool = Field(default=True, alias="FLARESOLVERR_AUTO")

    aria2_rpc_url: str = Field(
        default="http://akwarr-aria2:6800/jsonrpc", alias="ARIA2_RPC_URL"
    )
    aria2_secret: str = Field(default="P3TERX", alias="ARIA2_SECRET")
    preferred_qualities: str = Field(default="720p,1080p,480p", alias="PREFERRED_QUALITIES")

    movies_path: Path = Field(default=Path("/media/Movie/Arabic"), alias="MOVIES_PATH")
    series_path: Path = Field(default=Path("/media/Serries/Arabic"), alias="SERIES_PATH")
    staging_path: Path = Field(default=Path("/media/Download/akwarr-staging"), alias="STAGING_PATH")
    data_path: Path = Field(default=Path("/config"), alias="DATA_PATH")

    tmdb_api_key: str = Field(default="", alias="TMDB_API_KEY")
    metadata_language: str = Field(default="ar", alias="METADATA_LANGUAGE")
    save_akwam_artwork: bool = Field(default=True, alias="SAVE_AKWAM_ARTWORK")

    jellyfin_url: str = Field(default="http://127.0.0.1:8096", alias="JELLYFIN_URL")
    jellyfin_api_key: str = Field(default="", alias="JELLYFIN_API_KEY")
    jellyfin_movies_library_name: str = Field(
        default="Arabic Movies", alias="JELLYFIN_MOVIES_LIBRARY_NAME"
    )
    jellyfin_series_library_name: str = Field(
        default="Arabic Series", alias="JELLYFIN_SERIES_LIBRARY_NAME"
    )

    worker_poll_seconds: int = Field(default=10, alias="WORKER_POLL_SECONDS")
    max_active_downloads: int = Field(default=3, alias="MAX_ACTIVE_DOWNLOADS")
    file_mode: int = Field(default=0o664, alias="FILE_MODE")
    dir_mode: int = Field(default=0o775, alias="DIR_MODE")

    @property
    def quality_list(self) -> list[str]:
        return [q.strip() for q in self.preferred_qualities.split(",") if q.strip()]

    @property
    def db_path(self) -> Path:
        return self.data_path / "akwarr.db"

    @property
    def is_radarr(self) -> bool:
        return self.mode.lower() == "radarr"

    @property
    def is_sonarr(self) -> bool:
        return self.mode.lower() == "sonarr"

    @property
    def root_folder_path(self) -> Path:
        return self.movies_path if self.is_radarr else self.series_path


@lru_cache
def get_settings() -> Settings:
    return Settings()
