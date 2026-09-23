"""Watchlist persistence and pin/unpin semantics."""
import pytest


class TestToggle:
    def test_pin_then_unpin(self, sd):
        items, pinned = sd.toggle_watch([], "101", "train")
        assert pinned is True and len(items) == 1
        items, pinned = sd.toggle_watch(items, "101")
        assert pinned is False and items == []

    def test_records_name_and_timestamp(self, sd):
        items, _ = sd.toggle_watch([], "101", "train")
        assert items[0]["name"] == "train"
        assert items[0]["added"]

    def test_hostile_job_id_is_refused(self, sd):
        items, pinned = sd.toggle_watch([], "--version")
        assert (items, pinned) == ([], False)
        items, pinned = sd.toggle_watch([], "; rm -rf /")
        assert (items, pinned) == ([], False)

    def test_ids_set(self, sd):
        items, _ = sd.toggle_watch([], "1")
        items, _ = sd.toggle_watch(items, "2")
        assert sd.watchlist_ids(items) == {"1", "2"}


class TestPersistence:
    def test_round_trip(self, sd, tmp_path):
        path = tmp_path / "w.json"
        items, _ = sd.toggle_watch([], "101", "train")
        assert sd.save_watchlist(items, path)
        assert [e["jobid"] for e in sd.load_watchlist(path)] == ["101"]

    def test_file_is_private(self, sd, tmp_path):
        path = tmp_path / "w.json"
        sd.save_watchlist([{"jobid": "1", "name": "", "added": ""}], path)
        assert oct(path.stat().st_mode & 0o777) == "0o600"

    def test_missing_file(self, sd, tmp_path):
        assert sd.load_watchlist(tmp_path / "nope.json") == []

    @pytest.mark.parametrize("content", ['{"a": 1}', "not json", "[1, 2]", '"x"'])
    def test_corrupt_file_returns_empty(self, sd, tmp_path, content):
        path = tmp_path / "w.json"
        path.write_text(content)
        assert sd.load_watchlist(path) == []

    def test_load_drops_duplicates_and_bad_ids(self, sd, tmp_path):
        path = tmp_path / "w.json"
        path.write_text('[{"jobid":"1"},{"jobid":"1"},{"jobid":"; rm"},'
                        '{"no_jobid":1},{"jobid":"2"}]')
        assert [e["jobid"] for e in sd.load_watchlist(path)] == ["1", "2"]
