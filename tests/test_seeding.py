"""Importing history from sacct, and node co-tenancy."""
import pytest

SACCT_ROWS = "\n".join([
    "48100|COMPLETED|gpu|32|128G|cpu=32,mem=128G,gres/gpu=4|"
    "2026-09-01T09:00:00|2026-09-01T13:12:03|2026-09-01T08:55:00|llama-finetune",
    "48101|FAILED|gpu|16|64G|cpu=16,mem=64G,gres/gpu=2|"
    "2026-09-02T10:00:00|2026-09-02T10:04:00|2026-09-02T09:58:00|eval|sweep",
    "48102|TIMEOUT|cpu|8|32G|cpu=8|Unknown|Unknown|2026-09-03T11:00:00|prep",
])


class TestSacctImport:
    @pytest.fixture
    def jobs(self, sd, fake_slurm):
        fake_slurm.set("sacct", SACCT_ROWS)
        return sd.sacct_recent_jobs(30, "testuser")

    def test_returns_history_shaped_records(self, jobs):
        job = jobs[0]
        assert set(job) >= {"jobid", "name", "user", "partition", "cpus",
                            "mem", "gpus", "state", "first_seen", "last_seen",
                            "stdout", "stderr"}

    def test_maps_fields(self, jobs):
        job = jobs[0]
        assert job["jobid"] == "48100"
        assert job["state"] == "COMPLETED"
        assert job["partition"] == "gpu"
        assert job["cpus"] == "32"
        assert job["gpus"] == "4"
        assert job["first_seen"] == "2026-09-01 09:00:00"
        assert job["last_seen"] == "2026-09-01 13:12:03"

    def test_job_name_may_contain_the_separator(self, jobs):
        assert jobs[1]["name"] == "eval|sweep"

    def test_falls_back_to_submit_when_start_is_unknown(self, jobs):
        assert jobs[2]["first_seen"] == "2026-09-03 11:00:00"
        assert jobs[2]["last_seen"] == "2026-09-03 11:00:00"

    def test_no_gpu_leaves_the_field_empty(self, jobs):
        assert jobs[2]["gpus"] == ""

    def test_malformed_job_ids_are_dropped(self, sd, fake_slurm):
        fake_slurm.set("sacct",
            "bad-id|COMPLETED|cpu|1|1G|cpu=1|2026-09-04T11:00:00|"
            "2026-09-04T12:00:00|2026-09-04T10:00:00|x")
        assert sd.sacct_recent_jobs(30, "testuser") == []

    def test_disabled_by_zero_days(self, sd, fake_slurm):
        assert sd.sacct_recent_jobs(0, "testuser") == []
        assert fake_slurm.calls == []

    def test_hostile_username_never_reaches_sacct(self, sd, fake_slurm):
        assert sd.sacct_recent_jobs(30, "; rm -rf /") == []
        assert fake_slurm.calls == []

    def test_accounting_unavailable(self, sd, fake_slurm):
        fake_slurm.set("sacct", "")
        assert sd.sacct_recent_jobs(30, "testuser") == []


class TestMerge:
    def test_adds_only_unknown_jobs(self, sd, fake_slurm):
        fake_slurm.set("sacct", SACCT_ROWS)
        seeded = sd.sacct_recent_jobs(30, "testuser")
        history = [{"jobid": "48100", "name": "already here",
                    "first_seen": "2026-09-01 09:00:00"}]
        merged, added = sd.merge_seeded_history(history, seeded)
        assert added == 2
        assert len(merged) == 3

    def test_existing_records_win(self, sd, fake_slurm):
        """A live record may carry log paths and a cached submit line that
        sacct cannot supply; the import must not overwrite it."""
        fake_slurm.set("sacct", SACCT_ROWS)
        seeded = sd.sacct_recent_jobs(30, "testuser")
        history = [{"jobid": "48100", "name": "keep me",
                    "stdout": "/tmp/o.txt", "submit_line": "sbatch j.sh",
                    "first_seen": "2026-09-01 09:00:00"}]
        merged, _ = sd.merge_seeded_history(history, seeded)
        kept = next(e for e in merged if e["jobid"] == "48100")
        assert kept["stdout"] == "/tmp/o.txt"
        assert kept["submit_line"] == "sbatch j.sh"
        assert kept["name"] == "keep me"

    def test_trim_keeps_the_newest(self, sd, fake_slurm):
        fake_slurm.set("sacct", SACCT_ROWS)
        seeded = sd.sacct_recent_jobs(30, "testuser")
        merged, _ = sd.merge_seeded_history([], seeded, limit=2)
        assert [e["jobid"] for e in merged] == ["48101", "48102"]

    def test_nothing_to_add(self, sd):
        history = [{"jobid": "1", "first_seen": "2026-01-01 00:00:00"}]
        merged, added = sd.merge_seeded_history(history, [])
        assert (merged, added) == (history, 0)


class TestCoTenants:
    SQUEUE = "\n".join([
        "48213|testuser|RUNNING|32|128G|gpu:4|4:12:03|llama-finetune",
        "48213.batch|testuser|RUNNING|32|128G|gpu:4|4:12:03|batch",
        "48222|jlopez|RUNNING|4|16G|N/A|2:05:44|genome|align",
        "48230|mrivas|RUNNING|8|32G|gpu:1|0:30:00|infer",
    ])

    @pytest.fixture
    def others(self, sd, fake_slurm):
        fake_slurm.set("squeue", self.SQUEUE)
        return sd.get_node_cotenants("node07", "48213")

    def test_excludes_the_job_being_monitored(self, others):
        assert [c["jobid"] for c in others] == ["48222", "48230"]

    def test_excludes_the_jobs_own_steps(self, others):
        assert not any(c["jobid"].startswith("48213") for c in others)

    def test_excludes_array_tasks_of_the_same_job(self, sd, fake_slurm):
        fake_slurm.set("squeue",
            "900_3|testuser|RUNNING|4|16G|N/A|1:00|mine\n"
            "901|other|RUNNING|4|16G|N/A|1:00|theirs")
        assert [c["jobid"] for c in sd.get_node_cotenants("node07", "900")] == ["901"]

    def test_parses_each_tenant(self, others):
        first = others[0]
        assert first["user"] == "jlopez"
        assert first["cpus"] == "4"
        assert first["mem"] == "16G"
        assert first["gpus"] == ""
        assert first["name"] == "genome|align"

    def test_reads_gpu_count(self, others):
        assert others[1]["gpus"] == "1"

    def test_exclusive_node(self, sd, fake_slurm):
        fake_slurm.set("squeue",
            "48213|testuser|RUNNING|32|128G|gpu:4|4:12:03|solo")
        assert sd.get_node_cotenants("node07", "48213") == []

    def test_hostile_node_name_refused(self, sd, fake_slurm):
        assert sd.get_node_cotenants("-evil", "1") == []
        assert fake_slurm.calls == []

    def test_no_exclusion_lists_everything(self, sd, fake_slurm):
        fake_slurm.set("squeue", self.SQUEUE)
        assert len(sd.get_node_cotenants("node07")) == 4
