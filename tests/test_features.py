"""Config, templates, arrays, node metrics, priority, logs."""
import os
import pytest


class TestConfig:
    def test_shipped_template_reproduces_defaults(self, sd, tmp_path):
        """Regression: inline comments used to make every boolean False."""
        path = tmp_path / "config.ini"
        sd.write_default_config(path)
        cfg = sd.load_config(path)
        for section, values in sd.CONFIG_DEFAULTS.items():
            for key, default in values.items():
                assert cfg[section][key] == default, f"{section}.{key}"

    @pytest.mark.parametrize("raw,expected", [
        ("use_ssh = false", False),
        ("use_ssh = no", False),
        ("use_ssh = off", False),
        ("use_ssh = 0", False),
        ("use_ssh = true", True),
        ("use_ssh = yes  ; trailing", True),
        ("use_ssh = on # trailing", True),
        ("use_ssh = ture", True),      # typo keeps the default, never silent-off
    ])
    def test_boolean_parsing(self, sd, tmp_path, raw, expected):
        path = tmp_path / "c.ini"
        path.write_text(f"[monitor]\n{raw}\n")
        assert sd.load_config(path)["monitor"]["use_ssh"] is expected

    def test_values_are_clamped(self, sd, tmp_path):
        path = tmp_path / "c.ini"
        path.write_text("[general]\nrefresh_interval = 0\nmax_history = 1\n")
        cfg = sd.load_config(path)
        assert cfg["general"]["refresh_interval"] == 1
        assert cfg["general"]["max_history"] == 10

    def test_broken_file_falls_back(self, sd, tmp_path):
        path = tmp_path / "c.ini"
        path.write_text("}{ not ini")
        assert sd.load_config(path)["general"]["refresh_interval"] == 3

    def test_unknown_keys_ignored(self, sd, tmp_path):
        path = tmp_path / "c.ini"
        path.write_text("[general]\nnope = 1\n[bogus]\nx = 2\n")
        assert sd.load_config(path)["general"]["refresh_interval"] == 3

    def test_write_does_not_clobber(self, sd, tmp_path):
        path = tmp_path / "c.ini"
        path.write_text("[general]\nrefresh_interval = 9\n")
        sd.write_default_config(path)
        assert sd.load_config(path)["general"]["refresh_interval"] == 9


class TestTemplates:
    def test_round_trip(self, sd, tmp_path):
        path = tmp_path / "t.json"
        tpl = sd.upsert_template([], "gpu-run", {"script": "a.sh", "mem": "16G"})
        assert sd.save_templates(tpl, path)
        loaded = sd.load_templates(path)
        assert loaded[0]["name"] == "gpu-run"
        assert loaded[0]["values"]["mem"] == "16G"

    def test_upsert_replaces_by_name(self, sd):
        tpl = sd.upsert_template([], "a", {"mem": "1G"})
        tpl = sd.upsert_template(tpl, "a", {"mem": "2G"})
        assert len(tpl) == 1
        assert tpl[0]["values"]["mem"] == "2G"

    def test_unknown_fields_dropped(self, sd, tmp_path):
        path = tmp_path / "t.json"
        sd.save_templates([{"name": "x", "values": {"script": "a.sh", "evil": "1"}}], path)
        assert "evil" not in sd.load_templates(path)[0]["values"]

    def test_corrupt_file_returns_empty(self, sd, tmp_path):
        path = tmp_path / "t.json"
        path.write_text("{not json")
        assert sd.load_templates(path) == []
        path.write_text('{"a": 1}')
        assert sd.load_templates(path) == []
        path.write_text('[1, "x", {"no_name": 1}]')
        assert sd.load_templates(path) == []

    def test_missing_file(self, sd, tmp_path):
        assert sd.load_templates(tmp_path / "nope.json") == []

    def test_nameless_template_ignored(self, sd):
        assert sd.upsert_template([], "", {"script": "a.sh"}) == []


class TestArrayRerun:
    TASKS = [
        {"jobid": "9_1", "state": "FAILED"},
        {"jobid": "9_2", "state": "COMPLETED"},
        {"jobid": "9_3", "state": "TIMEOUT"},
        {"jobid": "9_4", "state": "FAILED"},
        {"jobid": "9_5", "state": "OUT_OF_MEMORY"},
        {"jobid": "9_8", "state": "CANCELLED by 42"},
        {"jobid": "9_9", "state": "RUNNING"},
    ]

    def test_selects_only_unsuccessful_tasks(self, sd):
        assert sd.failed_task_indices(self.TASKS) == [1, 3, 4, 5, 8]

    @pytest.mark.parametrize("indices,expected", [
        ([1, 2, 3], "1-3"),
        ([1, 3, 4, 5, 9], "1,3-5,9"),
        ([7], "7"),
        ([], ""),
        ([5, 1, 3, 2], "1-3,5"),
    ])
    def test_compacts_to_slurm_syntax(self, sd, indices, expected):
        assert sd.compact_indices(indices) == expected

    def test_replaces_existing_array_option(self, sd):
        assert sd.build_array_rerun_args(
            "sbatch --array=1-100 --mem=4G j.sh", [2, 3]
        ) == ["sbatch", "--array=2-3", "--mem=4G", "j.sh"]

    def test_replaces_separated_array_option(self, sd):
        assert sd.build_array_rerun_args(
            "sbatch -a 1-100 j.sh", [4]) == ["sbatch", "--array=4", "j.sh"]

    def test_adds_when_absent(self, sd):
        assert sd.build_array_rerun_args(
            "sbatch j.sh", [1]) == ["sbatch", "--array=1", "j.sh"]

    def test_refuses_non_sbatch_line(self, sd):
        assert sd.build_array_rerun_args("rm -rf /", [1]) is None

    def test_no_indices_means_nothing_to_do(self, sd):
        assert sd.build_array_rerun_args("sbatch j.sh", []) is None


class TestNodeMetricsWithoutSsh:
    LINE = ("NodeName=n1 State=MIXED CPUAlloc=4 CPUTot=8 CPULoad=3.50 "
            "RealMemory=64000 AllocMem=16000 FreeMem=40000 "
            "Gres=gpu:a100:4 GresUsed=gpu:a100:2(IDX:0-1)")

    def test_allocation_percentages(self, sd, fake_slurm):
        fake_slurm.set("scontrol", self.LINE)
        info = sd.get_node_info_scontrol("n1")
        assert info["cpu_pct"] == 50
        assert info["mem_pct"] == 25
        assert info["load"] == 3.5

    @pytest.mark.parametrize("gres,used,total,alloc", [
        ("Gres=gpu:4 GresUsed=gpu:1", None, 4, 1),
        ("Gres=gpu:a100:4 GresUsed=gpu:a100:2(IDX:0-1)", None, 4, 2),
        ("Gres=gpu:2(IDX:0-1) GresUsed=gpu:0", None, 2, 0),
        ("Gres=(null) GresUsed=(null)", None, 0, 0),
    ])
    def test_gpu_model_digits_are_not_the_count(self, sd, fake_slurm, gres, used,
                                                total, alloc):
        fake_slurm.set("scontrol", "NodeName=n1 CPUTot=8 RealMemory=1 " + gres)
        info = sd.get_node_info_scontrol("n1")
        assert (info["gpu_total"], info["gpu_alloc"]) == (total, alloc)

    def test_hostile_node_name_refused(self, sd, fake_slurm):
        assert sd.get_node_info_scontrol("-evil") == {}
        assert fake_slurm.calls == []

    def test_empty_output(self, sd, fake_slurm):
        fake_slurm.set("scontrol", "")
        assert sd.get_node_info_scontrol("n1") == {}


class TestPendingExplanations:
    def test_known_reasons(self, sd):
        assert "higher priority" in sd.explain_pending_reason("(Priority)")
        assert "never start" in sd.explain_pending_reason("DependencyNeverSatisfied")
        assert "busy" in sd.explain_pending_reason("Resources")

    def test_unknown_reason_is_empty(self, sd):
        assert sd.explain_pending_reason("SomethingNew") == ""
        assert sd.explain_pending_reason("") == ""

    def test_priority_breakdown(self, sd, fake_slurm):
        fake_slurm.set("sprio", "1234|5021|100|4200|20|500|200|1|0")
        prio = sd.get_job_priority("1234")
        assert prio["total"] == 5021
        assert prio["fairshare"] == 4200

    def test_priority_invalid_id(self, sd, fake_slurm):
        assert sd.get_job_priority("--evil") == {}
        assert fake_slurm.calls == []

    def test_queue_position(self, sd, fake_slurm):
        fake_slurm.set("squeue", "7|900\n8|800\n9|700")
        assert sd.get_priority_queue_position("8") == (2, 3)

    def test_fairshare(self, sd, fake_slurm):
        fake_slurm.set("sshare", "acct|testuser|1000|0.25|0.75")
        assert sd.get_fairshare()["fairshare"] == 0.75


class TestPartitionLimits:
    def test_parses_limits(self, sd, fake_slurm):
        fake_slurm.set("scontrol",
            "PartitionName=gpu MaxTime=2-00:00:00 MaxNodes=4 TotalCPUs=256 "
            "DefMemPerCPU=4000 State=UP Default=YES\n"
            "PartitionName=cpu MaxTime=1-00:00:00 State=UP Default=NO")
        parts = sd.get_partition_info()
        assert set(parts) == {"gpu", "cpu"}
        assert parts["gpu"]["max_time"] == "2-00:00:00"
        assert parts["gpu"]["def_mem_per_cpu"] == "4000"
        assert parts["gpu"]["default"] is True
        assert parts["cpu"]["default"] is False


class TestIncrementalLogReading:
    def test_first_read_returns_tail_and_resets(self, sd, tmp_path):
        f = tmp_path / "log"
        f.write_text("a\nb\n")
        lines, offset, reset = sd.follow_file(str(f), 0)
        assert (lines, reset) == (["a", "b"], True)
        assert offset == f.stat().st_size

    def test_only_new_bytes_are_read(self, sd, tmp_path):
        f = tmp_path / "log"
        f.write_text("a\n")
        _, offset, _ = sd.follow_file(str(f), 0)
        with open(f, "a") as fh:
            fh.write("b\n")
        lines, offset2, reset = sd.follow_file(str(f), offset)
        assert (lines, reset) == (["b"], False)
        assert offset2 > offset

    def test_incomplete_trailing_line_is_withheld(self, sd, tmp_path):
        f = tmp_path / "log"
        f.write_text("a\n")
        _, offset, _ = sd.follow_file(str(f), 0)
        with open(f, "a") as fh:
            fh.write("partial")
        lines, offset2, _ = sd.follow_file(str(f), offset)
        assert lines == []
        assert offset2 == offset          # re-read it next time, whole
        with open(f, "a") as fh:
            fh.write(" done\n")
        lines2, _, _ = sd.follow_file(str(f), offset2)
        assert lines2 == ["partial done"]

    def test_truncation_triggers_reset(self, sd, tmp_path):
        f = tmp_path / "log"
        f.write_text("old content here\n")
        _, offset, _ = sd.follow_file(str(f), 0)
        f.write_text("new\n")
        lines, _, reset = sd.follow_file(str(f), offset)
        assert (lines, reset) == (["new"], True)

    def test_missing_file(self, sd, tmp_path):
        assert sd.follow_file(str(tmp_path / "nope"), 0) == ([], 0, False)

    def test_no_change_reads_nothing(self, sd, tmp_path):
        f = tmp_path / "log"
        f.write_text("a\n")
        _, offset, _ = sd.follow_file(str(f), 0)
        assert sd.follow_file(str(f), offset) == ([], offset, False)


class TestLogFilter:
    @pytest.fixture
    def viewer(self, sd):
        v = sd.LogViewerModal.__new__(sd.LogViewerModal)
        v._errors_only = False
        return v

    def test_substring_is_case_insensitive(self, viewer):
        viewer._filter = "ERROR"
        assert viewer._matcher()("an error occurred") is True

    def test_regex_between_slashes(self, viewer):
        viewer._filter = "/^ERR\\d+/"
        assert viewer._matcher()("ERR42") is True
        assert viewer._matcher()("xERR42") is False

    def test_half_typed_regex_matches_inner_text(self, viewer):
        viewer._filter = "/[unclosed/"
        assert viewer._matcher()("a [unclosed b") is True

    def test_empty_filter_disables_matching(self, viewer):
        viewer._filter = ""
        assert viewer._matcher() is None

    @pytest.mark.parametrize("line,style", [
        ("Traceback (most recent call last)", "bold red"),
        ("CUDA error: out of memory", "bold red"),
        ("WARNING: deprecated", "yellow"),
        ("Job completed", "bold green"),
        ("ordinary output", "#c9d1d9"),
    ])
    def test_line_classification(self, sd, line, style):
        assert sd.LogViewerModal.line_style(line) == style
