"""Parsing of Slurm command output."""
import pytest


class TestSqueue:
    def test_fields_map_to_the_right_keys(self, sd, fake_slurm):
        fake_slurm.set("squeue",
            "101|gpu|alice|RUNNING|1:00:00|2:00:00|4|16G|gpu:2|node01|node01|train")
        job = sd.parse_squeue()[0]
        assert job["jobid"] == "101"
        assert job["partition"] == "gpu"
        assert job["user"] == "alice"
        assert job["state"] == "RUNNING"
        assert job["cpus"] == "4"
        assert job["gpus"] == "2"
        assert job["nodes"] == "node01"
        assert job["name"] == "train"

    def test_pipe_in_job_name_does_not_shift_columns(self, sd, fake_slurm):
        """A job name may contain the field separator. Everything after the
        last fixed field belongs to the name."""
        fake_slurm.set("squeue",
            "101|gpu|alice|RUNNING|1:00|2:00|4|16G|gpu:2|node01|node01|a|b|c")
        job = sd.parse_squeue()[0]
        assert job["name"] == "a|b|c"
        assert job["user"] == "alice"       # would be "a" if split were unbounded
        assert job["state"] == "RUNNING"

    def test_short_lines_are_skipped(self, sd, fake_slurm):
        fake_slurm.set("squeue", "garbage\n101|gpu|alice|R|1:00|2:00|4|16G||n1|n1|x")
        assert [j["jobid"] for j in sd.parse_squeue()] == ["101"]

    def test_reason_none_becomes_empty(self, sd, fake_slurm):
        fake_slurm.set("squeue", "101|gpu|alice|R|1:00|2:00|4|16G||None|n1|x")
        assert sd.parse_squeue()[0]["reason"] == ""

    def test_gpu_only_counted_for_gres_gpu(self, sd, fake_slurm):
        fake_slurm.set("squeue", "1|p|u|R|1:00|2:00|4|16G|billing=7|n|n|x")
        assert sd.parse_squeue()[0]["gpus"] == ""


class TestSinfo:
    def test_features_keep_embedded_separators(self, sd, fake_slurm):
        fake_slurm.set("sinfo", "n1|gpu|idle|0/8/0/8|64000|gpu:2|f1|f2")
        node = sd.parse_sinfo()[0]
        assert node["node"] == "n1"
        assert node["gres"] == "gpu:2"
        assert node["features"].startswith("f1")


class TestDurations:
    @pytest.mark.parametrize("text,seconds", [
        ("1-02:03:04", 93784.0),
        ("02:03:04", 7384.0),
        ("12:34.567", 754.567),
        ("45", 45.0),
        ("", 0.0),
        ("UNLIMITED", 0.0),
        ("N/A", 0.0),
        ("nonsense", 0.0),
    ])
    def test_parse_slurm_duration(self, sd, text, seconds):
        assert sd.parse_slurm_duration(text) == pytest.approx(seconds)


class TestMemory:
    @pytest.mark.parametrize("text,mb,scope", [
        ("16G", 16384.0, ""),
        ("4Gc", 4096.0, "c"),
        ("2Gn", 2048.0, "n"),
        ("1024K", 1.0, ""),
        ("500M", 500.0, ""),
        ("500", 500.0, ""),
        ("N/A", 0.0, ""),
        ("", 0.0, ""),
    ])
    def test_parse_mem(self, sd, text, mb, scope):
        assert sd.parse_mem(text) == (pytest.approx(mb), scope)


class TestTres:
    @pytest.mark.parametrize("text,gpus", [
        ("cpu=4,mem=16G,node=1,gres/gpu=2", 2),
        ("cpu=8,gres/gpu:a100=4", 4),
        ("cpu=4,mem=8G", 0),
        ("", 0),
    ])
    def test_gpu_count(self, sd, text, gpus):
        assert sd.tres_gpu_count(text) == gpus


class TestArrayExpansion:
    def test_task_name_may_contain_separator(self, sd, fake_slurm):
        fake_slurm.set("sacct",
            "9_1|COMPLETED|0:0|00:05|node01|s|e|my|name")
        task = sd.expand_array_job("9")[0]
        assert task["name"] == "my|name"
        assert task["state"] == "COMPLETED"
        assert task["exitcode"] == "0:0"

    def test_invalid_base_id_is_refused(self, sd, fake_slurm):
        assert sd.expand_array_job("; rm -rf /") == []


class TestFinalState:
    def test_terminal_state_wins_over_live_rows(self, sd, fake_slurm):
        fake_slurm.set("sacct", "5|RUNNING\n5.batch|FAILED\n")
        assert sd.sacct_final_state(["5"]) == {"5": "FAILED"}

    def test_ids_are_batched(self, sd, fake_slurm):
        fake_slurm.set("sacct", "")
        sd.sacct_final_state([str(i) for i in range(120)])
        assert len(fake_slurm.calls) == 3

    def test_invalid_ids_never_reach_sacct(self, sd, fake_slurm):
        fake_slurm.set("sacct", "")
        sd.sacct_final_state(["--evil", "1"])
        assert all("--evil" not in ",".join(c) for c in fake_slurm.calls)


class TestReasonColumn:
    def test_running_job_reason_is_blank(self, sd, fake_slurm):
        """%R holds the node list for a running job, which the NODES column
        already shows; repeating it reads as noise."""
        fake_slurm.set("squeue",
            "1|gpu|u|RUNNING|1:00|2:00|4|16G||node07|node07|train")
        job = sd.parse_squeue()[0]
        assert job["reason"] == ""
        assert job["nodes"] == "node07"

    def test_pending_job_keeps_its_reason(self, sd, fake_slurm):
        fake_slurm.set("squeue",
            "2|gpu|u|PENDING|0:00|1-00:00:00|4|16G||(Resources)||wait")
        job = sd.parse_squeue()[0]
        assert job["reason"] == "(Resources)"

    def test_day_long_time_left_fits_its_column(self, sd):
        """'1-00:00:00' is ten characters; the column must not clip it."""
        for table in (sd.SqueueTable, sd.MyJobsTable):
            for cols in (table.COLS_FULL, table.COLS_COMPACT, table.COLS_MINIMAL):
                width = dict(cols).get("TIME LEFT")
                if width is not None:
                    assert width >= 10, f"{table.__name__}: {width}"
