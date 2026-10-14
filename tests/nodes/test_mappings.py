"""Test access: library sampling, the access-check request, per-library results and mapping suggestions."""

from __future__ import annotations

import os

import pytest

from media_preview_generator.nodes import mappings as nm
from media_preview_generator.nodes.protocol import PathMapping, map_app_path


def _video(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _server(tmp_path, libraries, *, enabled=True, server_id="p1"):
    return {
        "id": server_id,
        "type": "plex",
        "name": "Home",
        "enabled": enabled,
        "url": "http://p:32400",
        "libraries": libraries,
        "path_mappings": [{"remote_prefix": "/plexmedia", "local_prefix": str(tmp_path / "media")}],
    }


class FakeClient:
    """Finds a file when ``visible(path)`` says so; records every request."""

    def __init__(self, visible=lambda path: True, nearby=None):
        self.visible = visible
        self.nearby = nearby or {}
        self.requests: list[tuple[list[PathMapping], list[dict]]] = []

    def access_check(self, mappings, files):
        self.requests.append((mappings, files))
        results = []
        for f in files:
            ok = self.visible(f["path"])
            results.append(
                {
                    "path": f["path"],
                    "ok": ok,
                    "node_path": map_app_path(f["path"], mappings) or "/node" + f["path"],
                    "kind": "" if ok else "missing",
                    "message": "" if ok else "Not found on the node",
                }
            )
        return {"ok": True, "results": results, "nearby": self.nearby}


class TestLibraryRoots:
    def test_enabled_libraries_resolve_through_the_servers_mappings(self, tmp_path):
        servers = [
            _server(
                tmp_path,
                [
                    {"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies", "/plexmedia/Movies"]},
                    {"id": "2", "name": "Off", "remote_paths": ["/plexmedia/Off"], "enabled": False},
                ],
            ),
            _server(tmp_path, [{"id": "9", "name": "X", "remote_paths": ["/plexmedia/X"]}], enabled=False),
            {"id": "bad", "type": "nonsense"},
        ]
        roots = nm.library_roots(servers)
        assert roots == [nm.LibraryRoot("Home", "p1", "Movies", "1", str(tmp_path / "media" / "Movies"))]


class TestSampleFiles:
    def test_it_picks_videos_breadth_first_with_size_and_mtime(self, tmp_path):
        root = tmp_path / "Movies"
        _video(root / "b.mkv", b"12345")
        _video(root / "notes.txt")
        _video(root / "A Film" / "a.MP4")
        _video(root / ".hidden" / "h.mkv")
        found = nm.sample_files(str(root))
        assert [os.path.basename(f["path"]) for f in found] == ["b.mkv", "a.MP4"]
        assert found[0]["size"] == 5
        assert found[0]["mtime_ns"] == (root / "b.mkv").stat().st_mtime_ns

    def test_it_stops_at_the_limit_and_the_walk_bound(self, tmp_path):
        root = tmp_path / "Movies"
        for i in range(6):
            _video(root / f"{i}.mkv")
        assert len(nm.sample_files(str(root))) == 3
        assert len(nm.sample_files(str(root), walk_limit=2)) == 2

    def test_a_missing_folder_gives_nothing(self, tmp_path):
        assert nm.sample_files(str(tmp_path / "nope")) == []


class TestRunTestAccess:
    def test_every_library_reports_its_files(self, tmp_path):
        _video(tmp_path / "media" / "Movies" / "a.mkv")
        _video(tmp_path / "media" / "TV" / "Show" / "s01e01.mkv")
        servers = [
            _server(
                tmp_path,
                [
                    {"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]},
                    {"id": "2", "name": "TV", "remote_paths": ["/plexmedia/TV"]},
                ],
            )
        ]
        rows = [PathMapping(str(tmp_path / "media"), "/mnt/media")]
        client = FakeClient(visible=lambda path: "Movies" in path)

        result = nm.run_test_access(client, rows, servers)

        assert len(client.requests) == 1 and client.requests[0][0] == rows
        assert [f["path"] for f in client.requests[0][1]] == [
            str(tmp_path / "media" / "Movies" / "a.mkv"),
            str(tmp_path / "media" / "TV" / "Show" / "s01e01.mkv"),
        ]
        movies, tv = result["libraries"]
        assert {k: movies[k] for k in ("server", "library", "app_root", "checked", "ok", "suggestion", "error")} == {
            "server": "Home",
            "library": "Movies",
            "app_root": str(tmp_path / "media" / "Movies"),
            "checked": 1,
            "ok": True,
            "suggestion": None,
            "error": None,
        }
        assert movies["files"] == [
            {
                "path": str(tmp_path / "media" / "Movies" / "a.mkv"),
                "ok": True,
                "kind": "",
                "message": "",
                "node_path": "/mnt/media/Movies/a.mkv",
            }
        ]
        assert (tv["ok"], tv["files"][0]["kind"], tv["files"][0]["message"]) == (
            False,
            "missing",
            "Not found on the node",
        )

    def test_a_missing_library_says_the_app_cant_open_it(self, tmp_path):
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        client = FakeClient()
        result = nm.run_test_access(client, [], servers)
        lib = result["libraries"][0]
        assert (lib["checked"], lib["ok"]) == (0, False)
        assert "can't open" in lib["error"]
        assert client.requests == []

    def test_an_empty_library_says_no_videos_were_found(self, tmp_path):
        (tmp_path / "media" / "Movies").mkdir(parents=True)
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        assert "No video files" in nm.run_test_access(FakeClient(), [], servers)["libraries"][0]["error"]

    def test_missing_files_get_a_suggestion_from_the_nodes_listing(self, tmp_path):
        _video(tmp_path / "media" / "Movies" / "a.mkv")
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        rows = [PathMapping(str(tmp_path / "media" / "Movies"), "/mnt/nas/films")]
        client = FakeClient(visible=lambda path: False, nearby={"/mnt/nas": ["Music", "MOVIES"]})
        lib = nm.run_test_access(client, rows, servers)["libraries"][0]
        assert lib["suggestion"] == {"from": "/mnt/nas/films", "to": "/mnt/nas/MOVIES"}

    def test_each_library_gets_only_the_listings_above_its_own_missing_files(self, tmp_path):
        _video(tmp_path / "media" / "Movies" / "a.mkv")
        _video(tmp_path / "media" / "TV" / "s01e01.mkv")
        servers = [
            _server(
                tmp_path,
                [
                    {"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]},
                    {"id": "2", "name": "TV", "remote_paths": ["/plexmedia/TV"]},
                ],
            )
        ]
        rows = [
            PathMapping(str(tmp_path / "media" / "Movies"), "/mnt/nas"),
            PathMapping(str(tmp_path / "media" / "TV"), "/srv/tv"),
        ]
        client = FakeClient(
            visible=lambda path: "TV" in path, nearby={"/mnt/nas": ["Movies", "TV"], "/elsewhere": ["x"]}
        )
        movies, tv = nm.run_test_access(client, rows, servers)["libraries"]
        assert movies["nearby"] == {"/mnt/nas": ["Movies", "TV"]}
        assert movies["suggestion"] == {"from": "/mnt/nas", "to": "/mnt/nas/Movies"}
        assert (tv["ok"], tv["nearby"], tv["suggestion"]) == (True, {}, None)

    def test_requests_are_split_into_batches(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nm, "CHECK_BATCH", 2)
        for i in range(3):
            _video(tmp_path / "media" / "Movies" / f"{i}.mkv")
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        client = FakeClient()
        assert nm.run_test_access(client, [], servers)["libraries"][0]["checked"] == 3
        assert [len(files) for _, files in client.requests] == [2, 1]


class TestSuggestRow:
    def test_a_root_below_the_mapped_folder_moves_the_rows_prefix_up_to_match(self):
        rows = [PathMapping("/data", "/srv/old")]
        suggestion = nm.suggest_row("/data/Movies", rows, {"/volume1/share": ["movies"]})
        assert suggestion == {"from": "/srv/old", "to": "/volume1/share"}

    def test_a_windows_node(self):
        rows = [PathMapping("/data/movies", "E:\\Movies")]
        assert nm.suggest_row("/data/movies", rows, {"D:\\": ["Movies"]}) == {"from": "E:\\Movies", "to": "D:\\Movies"}

    def test_a_windows_node_with_the_root_below_the_mapped_folder(self):
        rows = [PathMapping("/data", "E:\\Old")]
        suggestion = nm.suggest_row("/data/movies", rows, {"D:\\Media": ["Movies"]})
        assert suggestion == {"from": "E:\\Old", "to": "D:\\Media"}

    @pytest.mark.parametrize(
        ("rows", "nearby"),
        [
            ([], {"/mnt": ["Movies"]}),
            ([PathMapping("/data/Movies", "/mnt/Movies")], {"/mnt": ["Movies"]}),
            ([PathMapping("/data/Movies", "/mnt/x")], {"/mnt": ["Music"]}),
            ([PathMapping("/data/Movies", "/mnt/x")], "nonsense"),
        ],
        ids=["unmapped", "already-there", "no-match", "bad-listing"],
    )
    def test_no_suggestion(self, rows, nearby):
        assert nm.suggest_row("/data/Movies", rows, nearby) is None


class TestQuickAccessCheck:
    def test_one_file_per_row(self, tmp_path):
        for name in ("a.mkv", "b.mkv"):
            _video(tmp_path / "movies" / name)
        _video(tmp_path / "tv" / "c.mkv")
        rows = [PathMapping(str(tmp_path / "movies"), "/m"), PathMapping(str(tmp_path / "tv"), "/t")]
        client = FakeClient()
        assert nm.quick_access_check(client, rows) is True
        assert [os.path.basename(f["path"]) for f in client.requests[0][1]] == ["a.mkv", "c.mkv"]

    def test_a_file_the_node_cant_see_fails_it(self, tmp_path):
        _video(tmp_path / "movies" / "a.mkv")
        rows = [PathMapping(str(tmp_path / "movies"), "/m")]
        assert nm.quick_access_check(FakeClient(visible=lambda p: False), rows) is False

    def test_nothing_to_check_does_not_pass(self, tmp_path):
        assert nm.quick_access_check(FakeClient(), [PathMapping(str(tmp_path / "none"), "/m")]) is False


def test_default_mappings_are_one_row_per_top_library_folder(tmp_path):
    servers = [
        _server(
            tmp_path,
            [
                {"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]},
                {"id": "2", "name": "4K", "remote_paths": ["/plexmedia/Movies/4K"]},
                {"id": "3", "name": "TV", "remote_paths": ["/plexmedia/TV"]},
            ],
        )
    ]
    movies, tv = str(tmp_path / "media" / "Movies"), str(tmp_path / "media" / "TV")
    assert nm.default_mappings_for(servers) == [
        {"app_prefix": movies, "node_prefix": movies},
        {"app_prefix": tv, "node_prefix": tv},
    ]
