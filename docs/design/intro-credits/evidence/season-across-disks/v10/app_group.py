"""Scratch: the tree's own cross-disk season group (season.season_videos), for sflix's TV library as Plex lists it."""

import lib
from lib import S
from media_preview_generator.servers.base import Library, ServerConfig, ServerType

LIBRARY = ServerConfig(
    id="plex", type=ServerType.PLEX, name="Plex", enabled=True, url="http://plex", auth={},
    libraries=[Library("2", "TV Shows", tuple(f"{root}/TV Shows" for root in lib.ROOTS))],
)  # fmt: skip


def app_group(path: str) -> tuple[str, ...]:
    return S.season_group(path, S.season_videos(path, [LIBRARY])).episodes
