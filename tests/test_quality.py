from akwarr.config import Settings
from akwarr.core.quality import pick_order_for_quality, quality_for_profile_id, quality_profiles_payload
from akwarr.scraper.akwam import AkwamDownload, AkwamMetadata


def test_quality_for_profile_id_maps_from_preferred_list() -> None:
    settings = Settings(PREFERRED_QUALITIES="720p,1080p,480p")
    assert quality_for_profile_id(1, settings) == "720p"
    assert quality_for_profile_id(2, settings) == "1080p"
    assert quality_for_profile_id(3, settings) == "480p"
    assert quality_for_profile_id(99, settings) == "720p"


def test_pick_order_prefers_requested_then_fallback() -> None:
    settings = Settings(PREFERRED_QUALITIES="720p,1080p,480p")
    assert pick_order_for_quality("1080p", settings) == ["1080p", "720p", "480p"]


def test_quality_profiles_payload_follows_preferred_qualities() -> None:
    settings = Settings(PREFERRED_QUALITIES="720p,1080p,480p")
    profiles = quality_profiles_payload(settings)
    assert [p["name"] for p in profiles] == ["Arabic 720p", "Arabic 1080p", "Arabic 480p"]
    assert profiles[1]["items"][0]["quality"]["name"] == "1080p"


async def test_pick_download_honors_requested_quality() -> None:
    from akwarr.scraper.akwam import AkwamScraper

    class FakeFetcher:
        pass

    scraper = AkwamScraper(Settings())
    scraper.fetcher = FakeFetcher()
    meta = AkwamMetadata(
        title="Test",
        url="https://akwam.it/movie/1/test",
        kind="movie",
        poster=None,
        fanart=None,
        overview=None,
        year="2026",
        downloads=[
            AkwamDownload(quality="720p", size=None, link_url="https://akwam.it/link/720"),
            AkwamDownload(quality="1080p", size=None, link_url="https://akwam.it/link/1080"),
        ],
        episodes=[],
    )

    quality, link = await scraper.pick_download(meta, qualities=["1080p", "720p"])
    assert quality == "1080p"
    assert link == "https://akwam.it/link/1080"
