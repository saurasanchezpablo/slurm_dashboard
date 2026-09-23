"""Reservation parsing and the 'can I use it' distinction."""
from datetime import datetime

import pytest

RESERVATIONS = "\n".join([
    "ReservationName=maint StartTime=2026-09-24T08:00:00 EndTime=2026-09-24T18:00:00 "
    "Duration=10:00:00 Nodes=node[01-10] NodeCnt=10 CoreCnt=80 Features=(null) "
    "PartitionName=(null) Flags=MAINT,SPEC_NODES TRES=cpu=80 Users=root "
    "Accounts=(null) State=INACTIVE",
    "ReservationName=course StartTime=2026-09-23T09:00:00 EndTime=2026-09-23T17:00:00 "
    "Duration=08:00:00 Nodes=gpu[01-02] NodeCnt=2 CoreCnt=16 PartitionName=gpu "
    "Flags=IGNORE_JOBS TRES=cpu=16 Users=alice,testuser Accounts=proj1 State=ACTIVE",
])
NOW = datetime(2026, 9, 23, 12, 0, 0)


@pytest.fixture
def reservations(sd, fake_slurm):
    fake_slurm.set("scontrol", RESERVATIONS)
    return sd.parse_reservations()


class TestParsing:
    def test_reads_every_reservation(self, reservations):
        assert [r["name"] for r in reservations] == ["maint", "course"]

    def test_fields(self, reservations):
        maint = reservations[0]
        assert maint["nodes"] == "node[01-10]"
        assert maint["node_cnt"] == "10"
        assert maint["flags"] == ["MAINT", "SPEC_NODES"]
        assert maint["users"] == ["root"]

    def test_null_lists_become_empty(self, reservations):
        assert reservations[0]["accounts"] == []

    def test_comma_lists_are_split(self, reservations):
        assert reservations[1]["users"] == ["alice", "testuser"]
        assert reservations[1]["accounts"] == ["proj1"]

    @pytest.mark.parametrize("output", [
        "No reservations in the system",
        "",
        "   ",
        "a line with no key values",
    ])
    def test_degenerate_output(self, sd, fake_slurm, output):
        fake_slurm.set("scontrol", output)
        assert sd.parse_reservations() == []


class TestOwnership:
    def test_listed_user_may_submit(self, sd, reservations):
        assert sd.reservation_is_mine(reservations[1], "testuser") is True

    def test_other_users_reservation_is_not_mine(self, sd, reservations):
        assert sd.reservation_is_mine(reservations[0], "testuser") is False

    def test_account_membership_counts(self, sd, reservations):
        assert sd.reservation_is_mine(reservations[1], "nobody", ["proj1"]) is True

    def test_wrong_account(self, sd, reservations):
        assert sd.reservation_is_mine(reservations[1], "nobody", ["other"]) is False

    def test_no_accounts_argument(self, sd, reservations):
        assert sd.reservation_is_mine(reservations[0], "nobody") is False


class TestBlocking:
    def test_maint_without_ignore_jobs_blocks(self, sd, reservations):
        assert sd.reservation_blocks_jobs(reservations[0]) is True

    def test_ignore_jobs_does_not_block(self, sd, reservations):
        assert sd.reservation_blocks_jobs(reservations[1]) is False

    def test_maint_with_ignore_jobs_does_not_block(self, sd):
        assert sd.reservation_blocks_jobs(
            {"flags": ["MAINT", "IGNORE_JOBS"]}) is False


class TestStatus:
    def test_active_window_shows_remaining(self, sd, reservations):
        label, style = sd.reservation_status(reservations[1], NOW)
        assert label == "active, 5h left"
        assert "green" in style

    def test_future_window_shows_countdown(self, sd, reservations):
        assert sd.reservation_status(reservations[0], NOW)[0] == "starts in 20h"

    def test_past_window(self, sd, reservations):
        assert sd.reservation_status(
            reservations[0], datetime(2026, 9, 25))[0] == "ended"

    def test_unparseable_times_fall_back_to_state(self, sd):
        label, _ = sd.reservation_status(
            {"start_time": "Unknown", "end_time": "Unknown", "state": "ACTIVE"}, NOW)
        assert label == "active"


class TestAccounts:
    def test_reads_sacctmgr(self, sd, fake_slurm):
        fake_slurm.set("sacctmgr", "proj1\nproj2\nproj1\n")
        assert sd.get_my_accounts() == ["proj1", "proj2"]

    def test_falls_back_to_sshare(self, sd, fake_slurm):
        fake_slurm.set("sacctmgr", "")
        fake_slurm.set("sshare", "fallbackacct|testuser|1|0.1|0.5")
        assert sd.get_my_accounts() == ["fallbackacct"]

    def test_no_accounting_at_all(self, sd, fake_slurm):
        assert sd.get_my_accounts() == []


class TestTimeHelpers:
    @pytest.mark.parametrize("seconds,text", [
        (30, "30s"), (45 * 60, "45m"), (3600, "1h"),
        (5 * 3600 + 20 * 60, "5h 20m"),
        (2 * 86400 + 4 * 3600, "2d 4h"), (86400, "1d"),
    ])
    def test_humanize(self, sd, seconds, text):
        assert sd.humanize_delta(seconds) == text

    @pytest.mark.parametrize("text", ["Unknown", "N/A", "(null)", "", "garbage"])
    def test_unparseable_datetimes(self, sd, text):
        assert sd.parse_slurm_datetime(text) is None

    def test_iso_datetime(self, sd):
        assert sd.parse_slurm_datetime("2026-09-24T08:00:00") == datetime(
            2026, 9, 24, 8, 0, 0)
