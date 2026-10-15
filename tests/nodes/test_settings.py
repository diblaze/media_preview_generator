"""The ``nodes`` list: validation, key masking, lookups and the settings manager's revisioned write."""

from __future__ import annotations

import pytest

from media_preview_generator.nodes import settings as ns
from media_preview_generator.web.settings_manager import NodesConflict, SettingsManager


def _post(**fields):
    return {"name": "nuc", "url": "http://nuc.lan:9494", "key": "k-123", "path_mappings": [], **fields}


class TestValidateNode:
    def test_a_new_node_gets_an_id_and_every_field(self):
        record, err = ns.validate_node(_post(), existing=None, others=[])
        assert err == ""
        assert len(record["id"]) == 32
        assert record["node_uid"] is None
        assert (record["name"], record["url"], record["key"], record["enabled"]) == (
            "nuc",
            "http://nuc.lan:9494",
            "k-123",
            True,
        )
        assert record["path_mappings"] == [] and record["added_at"]

    @pytest.mark.parametrize(
        "url",
        ["ftp://nuc:9494", "nuc:9494", "http://", "http://u:p@nuc:9494", "http://nuc?x=1", "http://nuc#f", ""],
        ids=["scheme", "no-scheme", "no-host", "credentials", "query", "fragment", "empty"],
    )
    def test_addresses_it_refuses(self, url):
        record, err = ns.validate_node(_post(url=url), existing=None, others=[])
        assert record is None and "address" in err

    @pytest.mark.parametrize(
        "url",
        [
            "http://[::1",
            "http://nuc:99999",
            "http://nuc:abc",
            "http://nuc:0",
            "http://nuc lan:9494",
            "http://nuc:9494#",
            "http://nuc:9494?",
            9494,
            "plex-host:9494",
        ],
        ids=["open-bracket", "port-too-big", "port-not-a-number", "port-0", "space", "bare-#", "bare-?", "int", "host"],
    )
    def test_addresses_that_could_never_answer_are_refused_without_raising(self, url):
        assert ns.normalise_url(url)[0] is None

    @pytest.mark.parametrize("url", ["http://[::1]:9494", "https://nuc.lan", "http://10.0.0.5:65535"])
    def test_plain_addresses_are_kept(self, url):
        assert ns.normalise_url(url) == (url, "")

    @pytest.mark.parametrize(
        "url", ["http://nuc:9494/v1", "http://nuc:9494/v1/", "http://nuc:9494/v1/health", "http://nuc:9494/v1/info"]
    )
    def test_a_node_route_pasted_after_the_address_is_dropped(self, url):
        assert ns.normalise_url(url) == ("http://nuc:9494", "")

    def test_a_proxy_path_before_the_route_is_kept(self):
        assert ns.normalise_url("https://proxy.lan/node/v1/") == ("https://proxy.lan/node", "")

    def test_a_trailing_slash_is_dropped(self):
        record, _ = ns.validate_node(_post(url="https://nuc.lan:9494/"), existing=None, others=[])
        assert record["url"] == "https://nuc.lan:9494"

    @pytest.mark.parametrize("name", ["", "   "])
    def test_a_name_is_required(self, name):
        record, err = ns.validate_node(_post(name=name), existing=None, others=[])
        assert record is None and "name" in err

    def test_names_are_unique_ignoring_case(self):
        record, err = ns.validate_node(_post(name="NUC"), existing=None, others=[{"name": "nuc"}])
        assert record is None and "already called" in err

    @pytest.mark.parametrize(
        "key", ["", "has space", "curly\u2019quote", "k" * 201], ids=["empty", "space", "non-ascii", "long"]
    )
    def test_keys_it_refuses(self, key):
        record, err = ns.validate_node(_post(key=key), existing=None, others=[])
        assert record is None and "key" in err

    def test_the_mask_on_a_new_node_is_not_a_key(self):
        record, err = ns.validate_node(_post(key=ns.KEY_MASK), existing=None, others=[])
        assert record is None and "key" in err

    def test_the_mask_keeps_the_stored_key_and_the_ids_are_never_taken_from_the_client(self):
        stored, _ = ns.validate_node(_post(), existing=None, others=[])
        stored["node_uid"] = "uid-1"
        posted = {"id": "evil", "node_uid": "other", "key": ns.KEY_MASK, "name": "renamed"}
        record, err = ns.validate_node(posted, existing=stored, others=[])
        assert err == ""
        assert (record["id"], record["node_uid"], record["key"], record["name"]) == (
            stored["id"],
            "uid-1",
            "k-123",
            "renamed",
        )
        assert record["added_at"] == stored["added_at"]

    def test_fields_left_out_keep_their_stored_value(self):
        stored, _ = ns.validate_node(
            _post(path_mappings=[{"app_prefix": "/data", "node_prefix": "/mnt"}]), existing=None, others=[]
        )
        record, err = ns.validate_node({"enabled": False}, existing=stored, others=[])
        assert err == "" and record["enabled"] is False
        assert record["path_mappings"] == stored["path_mappings"] and record["url"] == stored["url"]

    @pytest.mark.parametrize(
        ("url", "kept"),
        [("http://nuc.lan:9494/", True), ("http://moved.lan:9494", False)],
        ids=["same-address", "new-address"],
    )
    def test_a_new_address_forgets_the_node_id_recorded_at_the_old_one(self, url, kept):
        stored, _ = ns.validate_node(_post(), existing=None, others=[])
        stored["node_uid"] = "uid-1"
        record, err = ns.validate_node({"url": url, "key": ns.KEY_MASK}, existing=stored, others=[])
        assert err == ""
        assert record["node_uid"] == ("uid-1" if kept else None)

    @pytest.mark.parametrize("key", [None, ns.KEY_MASK], ids=["left-out", "masked"])
    def test_saving_without_typing_the_key_keeps_the_node_id(self, key):
        stored, _ = ns.validate_node(_post(), existing=None, others=[])
        stored["node_uid"] = "uid-1"
        post = {"name": "renamed"} if key is None else {"name": "renamed", "key": key}
        record, err = ns.validate_node(post, existing=stored, others=[])
        assert err == "" and record["node_uid"] == "uid-1"

    def test_typing_the_key_again_at_the_same_address_forgets_the_node_id(self):
        # How the user confirms a Plex database node that reports a new id: it is recorded on its next answer.
        stored, _ = ns.validate_node(_post(), existing=None, others=[])
        stored["node_uid"] = "uid-1"
        record, err = ns.validate_node({"key": stored["key"]}, existing=stored, others=[])
        assert err == "" and record["node_uid"] is None

    def test_enabled_must_be_a_boolean(self):
        record, err = ns.validate_node(_post(enabled="yes"), existing=None, others=[])
        assert record is None and "enabled" in err


class TestMappings:
    def test_a_windows_node_folder_is_fine(self):
        rows, err = ns.normalise_mappings([{"app_prefix": "/data/movies", "node_prefix": "D:\\Movies"}])
        assert err == "" and rows == [{"app_prefix": "/data/movies", "node_prefix": "D:\\Movies"}]

    @pytest.mark.parametrize(
        ("row", "says"),
        [
            ({"app_prefix": "data", "node_prefix": "/mnt"}, "start with /"),
            ({"app_prefix": "/data", "node_prefix": ""}, "Enter the folder"),
            ("not-a-row", "object"),
        ],
        ids=["relative-app", "empty-node", "not-an-object"],
    )
    def test_rows_it_refuses(self, row, says):
        rows, err = ns.normalise_mappings([row])
        assert rows is None and says in err

    def test_the_same_app_folder_twice_is_refused(self):
        rows, err = ns.normalise_mappings(
            [{"app_prefix": "/data/", "node_prefix": "/a"}, {"app_prefix": "/data", "node_prefix": "/b"}]
        )
        assert rows is None and "twice" in err

    def test_not_a_list_is_refused(self):
        assert ns.normalise_mappings("x")[0] is None

    def test_rows_become_path_mappings(self):
        node = {"path_mappings": [{"app_prefix": "/data", "node_prefix": "/mnt"}, {"bad": 1}]}
        assert [m.to_json() for m in ns.node_mappings(node)] == [{"app_prefix": "/data", "node_prefix": "/mnt"}]


class TestMaskAndLookup:
    def test_masking_replaces_the_key_and_never_mutates_the_input(self):
        node = {"id": "a", "key": "secret"}
        assert ns.mask_node(node)["key"] == ns.KEY_MASK
        assert node["key"] == "secret"

    def test_find_node(self):
        nodes = [{"id": "a"}, {"id": "b"}]
        assert ns.find_node(nodes, "b") == {"id": "b"}
        assert ns.find_node(nodes, "c") is None
        assert ns.find_node("nonsense", "a") is None
        assert ns.find_node(nodes, None) is None


class TestValidateNodes:
    def test_duplicate_ids_are_refused(self):
        a, _ = ns.validate_node(_post(), existing=None, others=[])
        b = {**a, "name": "other"}
        nodes, err = ns.validate_nodes([a, b])
        assert nodes is None and "twice" in err

    def test_a_stored_list_keeps_ids_uids_and_keys(self):
        a, _ = ns.validate_node(_post(), existing=None, others=[])
        a["node_uid"] = "uid"
        nodes, err = ns.validate_nodes([a])
        assert err == "" and nodes == [a]


class TestSettingsManagerNodes:
    def test_update_nodes_bumps_the_revision_and_refuses_a_stale_editor(self, tmp_path):
        sm = SettingsManager(str(tmp_path))
        record, _ = ns.validate_node(_post(), existing=None, others=[])
        assert sm.nodes == [] and sm.nodes_revision == 0
        assert sm.update_nodes([record]) == 1
        assert sm.nodes == [record]
        with pytest.raises(NodesConflict):
            sm.update_nodes([record], expected_revision=0)
        assert sm.update_nodes([record], expected_revision=1) == 2

    def test_an_invalid_list_is_refused_and_nothing_is_saved(self, tmp_path):
        sm = SettingsManager(str(tmp_path))
        with pytest.raises(ValueError):
            sm.update_nodes([{"id": "a", "name": "", "url": "http://x", "key": "k"}])
        assert sm.get("nodes") is None

    def test_the_nodes_property_is_a_copy(self, tmp_path):
        sm = SettingsManager(str(tmp_path))
        record, _ = ns.validate_node(_post(), existing=None, others=[])
        sm.update_nodes([record])
        sm.nodes[0]["name"] = "changed"
        assert sm.nodes[0]["name"] == "nuc"

    def test_node_address_reads_the_stored_record(self, tmp_path):
        from media_preview_generator.web.settings_manager import get_settings_manager, reset_settings_manager

        reset_settings_manager()
        try:
            sm = get_settings_manager(str(tmp_path))
            record, _ = ns.validate_node(_post(), existing=None, others=[])
            sm.update_nodes([record])
            assert ns.node_address(record["id"]) == ("http://nuc.lan:9494", "k-123", "nuc")
            assert ns.node_address("missing") == ("", "", "")
            assert ns.node_address(None) == ("", "", "")
        finally:
            reset_settings_manager()
