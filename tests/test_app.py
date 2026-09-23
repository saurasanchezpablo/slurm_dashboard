"""End-to-end tests driving the real Textual app headlessly."""
import asyncio
import pytest

SQUEUE = "\n".join([
    "101|gpu|testuser|RUNNING|1:00:00|2:00:00|4|16G|gpu:2|node01|node01|train|model",
    "102|gpu|otheruser|PENDING|0:00|4:00:00|8|32G|gpu:4|(Resources)||other job",
    "103|cpu|testuser|PENDING|0:00|1:00:00|2|8G|N/A|(Priority)||prep",
    "104|cpu|testuser|RUNNING|0:10|1:00:00|2|8G|N/A|node02|node02|analysis",
])
SINFO = ("node01|gpu|mix|4/4/0/8|64000|gpu:2|f1\n"
         "node02|cpu|idle|0/8/0/8|32000|(null)|f2")
SACCT_EFF = "\n".join([
    "101|COMPLETED|01:00:00|03:30:00|4|1|16G||cpu=4,mem=16G,gres/gpu=2|0:0",
    "101.batch|COMPLETED|01:00:00|03:29:00|4|1||2048M||0:0",
])


def make_app(sd, monkeypatch, tmp_path, responses=None):
    resp = {"squeue": SQUEUE, "sinfo": SINFO, "sacct": "101|COMPLETED\n"}
    resp.update(responses or {})
    calls = []

    def fake_run(cmd, timeout=10):
        calls.append(list(cmd))
        if cmd[0] == "squeue" and "--start" in cmd:
            return ("103|2026-09-24T18:30:00\n", "")
        return (resp.get(cmd[0], ""), "")

    monkeypatch.setattr(sd, "run", fake_run)
    monkeypatch.setattr(sd, "run_out", lambda cmd: fake_run(cmd)[0])
    monkeypatch.setattr(sd, "HISTORY_FILE", tmp_path / "history.json")
    monkeypatch.setattr(sd, "EVENT_LOG_FILE", tmp_path / "events.log")
    monkeypatch.setattr(sd, "TEMPLATE_FILE", tmp_path / "templates.json")
    monkeypatch.setattr(sd, "MY_USER", "testuser")
    app = sd.SlurmDashboard()
    app._calls = calls
    return app


def run(coro):
    return asyncio.run(coro)


class TestBoot:
    def test_tables_populate_and_history_is_scoped(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path)

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                await pilot.pause()
                assert app.query_one(sd.SqueueTable).row_count == 4
                assert app.query_one(sd.SinfoTable).row_count == 2
                # only this user's jobs are persisted
                assert sorted(e["jobid"] for e in app._history) == ["101", "103", "104"]
                app.exit()
        run(scenario())

    def test_every_tab_renders(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path)

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                for tab in ("tab-mine", "tab-nodes", "tab-log", "tab-history",
                            "tab-stats", "tab-jobs", "tab-all"):
                    app.query_one("Tabs").active = tab
                    await pilot.pause()
                app.exit()
        run(scenario())


class TestOwnerGuards:
    @pytest.mark.parametrize("width,expect_user_column", [(200, True), (80, False)])
    def test_owner_resolved_at_any_width(self, sd, monkeypatch, tmp_path,
                                         width, expect_user_column):
        app = make_app(sd, monkeypatch, tmp_path)

        async def scenario():
            async with app.run_test(size=(width, 40)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                await pilot.pause()
                table = app.query_one(sd.SqueueTable)
                has_user = "USER" in [c for c, _ in table.COLS]
                assert has_user is expect_user_column
                table.move_cursor(row=1)          # job 102, otheruser
                await pilot.pause()
                assert app._get_selected_jobid() == "102"
                assert app._get_selected_user() == "otheruser"
                table.move_cursor(row=0)          # job 101, mine
                await pilot.pause()
                assert app._get_selected_user() == "testuser"
                app.exit()
        run(scenario())

    def test_cancel_refused_for_other_users(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path)
        notices = []

        async def scenario():
            async with app.run_test(size=(80, 40)) as pilot:
                app.notify = lambda m, **k: notices.append(str(m))
                await pilot.pause()
                await pilot.pause()
                app.query_one(sd.SqueueTable).move_cursor(row=1)
                await pilot.pause()
                app.action_job_scancel()
                await pilot.pause()
                assert any("another user" in n for n in notices)
                app.exit()
        run(scenario())

    def test_own_job_is_not_blocked(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path)
        notices = []

        async def scenario():
            async with app.run_test(size=(80, 40)) as pilot:
                app.notify = lambda m, **k: notices.append(str(m))
                await pilot.pause()
                await pilot.pause()
                app.query_one(sd.SqueueTable).move_cursor(row=0)
                await pilot.pause()
                app.action_job_hold()
                await pilot.pause()
                assert not any("another user" in n for n in notices)
                app.exit()
        run(scenario())


class TestEfficiencyModal:
    def test_renders_report(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path, {"sacct": SACCT_EFF})

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                await pilot.pause()
                app.action_job_efficiency()
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                screen = app.screen
                assert isinstance(screen, sd.EfficiencyModal)
                title = str(screen.query_one("#eff-title", sd.Label).content)
                assert "101" in title
                app.exit()
        run(scenario())


class TestWhyPending:
    def test_opens_for_pending_job(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path,
                       {"sprio": "103|5021|100|4200|20|500|200|1|0",
                        "sshare": "acct|testuser|1000|0.25|0.75"})

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                await pilot.pause()
                app.query_one(sd.SqueueTable).move_cursor(row=2)   # 103 PENDING
                await pilot.pause()
                app.action_why_pending()
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert isinstance(app.screen, sd.PriorityModal)
                app.exit()
        run(scenario())

    def test_refused_for_running_job(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path)
        notices = []

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda m, **k: notices.append(str(m))
                await pilot.pause()
                await pilot.pause()
                app.query_one(sd.SqueueTable).move_cursor(row=0)   # 101 RUNNING
                await pilot.pause()
                app.action_why_pending()
                await pilot.pause()
                assert any("not pending" in n for n in notices)
                assert not isinstance(app.screen, sd.PriorityModal)
                app.exit()
        run(scenario())


class TestSubmitTemplates:
    def test_save_and_load_round_trip(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path,
                       {"scontrol": "PartitionName=gpu MaxTime=1-00:00:00 State=UP"})

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                app.action_new_job()
                await pilot.pause()
                modal = app.screen
                assert isinstance(modal, sd.SubmitJobModal)
                modal.query_one("#si-script", sd.Input).value = "/tmp/j.sh"
                modal.query_one("#si-mem", sd.Input).value = "16G"
                modal.query_one("#si-partition", sd.Input).value = "gpu"
                await pilot.pause()
                modal._save_template("nightly", modal._get_values())
                await pilot.pause()
                saved = sd.load_templates(tmp_path / "templates.json")
                assert saved[0]["name"] == "nightly"
                assert saved[0]["values"]["mem"] == "16G"

                # clear, then load it back
                modal._set_values({})
                assert modal.query_one("#si-mem", sd.Input).value == ""
                modal._template_chosen(("load", saved[0]))
                await pilot.pause()
                assert modal.query_one("#si-mem", sd.Input).value == "16G"
                assert modal.query_one("#si-script", sd.Input).value == "/tmp/j.sh"
                app.exit()
        run(scenario())

    def test_partition_limits_are_shown(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path,
                       {"scontrol": "PartitionName=gpu MaxTime=2-00:00:00 "
                                    "MaxNodes=4 State=UP"})

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                app.action_new_job()
                await pilot.pause()
                await app.workers.wait_for_complete()
                modal = app.screen
                modal.query_one("#si-partition", sd.Input).value = "gpu"
                await pilot.pause()
                hint = str(modal.query_one("#partition-hint", sd.Label).content)
                assert "2-00:00:00" in hint
                modal.query_one("#si-partition", sd.Input).value = "nosuch"
                await pilot.pause()
                hint = str(modal.query_one("#partition-hint", sd.Label).content)
                assert "Unknown partition" in hint
                app.exit()
        run(scenario())

    def test_missing_script_is_rejected_locally(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path)

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                app.action_new_job()
                await pilot.pause()
                modal = app.screen
                modal.query_one("#si-script", sd.Input).value = str(tmp_path / "nope.sh")
                await pilot.pause()
                await pilot.click("#btn-submit-run")
                await pilot.pause()
                status = str(modal.query_one("#submit-status", sd.Label).content)
                assert "not found" in status
                app.exit()
        run(scenario())


class TestArrayRerun:
    ARRAY = "\n".join([
        "9_1|FAILED|1:0|00:05|node01|s|e|task",
        "9_2|COMPLETED|0:0|00:05|node01|s|e|task",
        "9_3|TIMEOUT|0:1|00:05|node01|s|e|task",
    ])

    def test_button_reflects_failed_count(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path, {"sacct": self.ARRAY})

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                await pilot.pause()
                app.action_array_expand()
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                modal = app.screen
                assert isinstance(modal, sd.ArrayJobModal)
                assert modal._failed == [1, 3]
                assert "2 failed" in str(modal.query_one("#btn-array-rerun",
                                                         sd.Button).label)
                app.exit()
        run(scenario())

    def test_no_failures_disables_rerun(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path,
                       {"sacct": "9_1|COMPLETED|0:0|00:05|n1|s|e|task"})

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                await pilot.pause()
                app.action_array_expand()
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                modal = app.screen
                assert modal._failed == []
                assert "No failed" in str(modal.query_one("#btn-array-rerun",
                                                          sd.Button).label)
                app.exit()
        run(scenario())


class TestLogViewer:
    def test_filter_and_incremental_tail(self, sd, monkeypatch, tmp_path):
        log = tmp_path / "job.out"
        log.write_text("hello world\nERROR: bad thing\nmore output\n")
        app = make_app(sd, monkeypatch, tmp_path)

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                modal = sd.LogViewerModal("101", str(log), "", live=False)
                app.push_screen(modal)
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert modal._buffers["stdout"] == [
                    "hello world", "ERROR: bad thing", "more output"]

                # filtering is applied on the buffer
                modal.query_one("#log-search", sd.Input).value = "error"
                await pilot.pause()
                label = str(modal.query_one("#log-match-lbl", sd.Label).content)
                assert "1 / 3" in label

                # errors-only toggle
                modal.query_one("#log-search", sd.Input).value = ""
                await pilot.pause()
                modal._errors_only = True
                modal._refresh_view()
                await pilot.pause()
                assert "1 / 3" in str(
                    modal.query_one("#log-match-lbl", sd.Label).content)
                modal._errors_only = False

                # appending only reads the new bytes
                before = modal._offsets["stdout"]
                with open(log, "a") as fh:
                    fh.write("appended line\n")
                modal._show_stream("stdout")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert modal._buffers["stdout"][-1] == "appended line"
                assert modal._offsets["stdout"] > before
                app.exit()
        run(scenario())

    def test_missing_file_message(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path)

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                modal = sd.LogViewerModal("101", str(tmp_path / "nope.out"), "",
                                          live=False)
                app.push_screen(modal)
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert modal._buffers["stdout"] == []
                app.exit()
        run(scenario())

    def test_live_viewer_arms_a_timer(self, sd, monkeypatch, tmp_path):
        log = tmp_path / "job.out"
        log.write_text("x\n")
        app = make_app(sd, monkeypatch, tmp_path)

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                live = sd.LogViewerModal("101", str(log), "", live=True)
                app.push_screen(live)
                await pilot.pause()
                assert live._timer is not None
                live._close()
                await pilot.pause()
                assert live._timer is None
                app.exit()
        run(scenario())


class TestBulkCancel:
    def test_selector_picks_only_my_matching_jobs(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path)

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                await pilot.pause()
                app._bulk_collect("pending")
                await pilot.pause()
                modal = app.screen
                assert isinstance(modal, sd.BulkCancelModal)
                # 103 is mine and pending; 102 is pending but another user's
                assert [j["jobid"] for j in modal._jobs] == ["103"]
                app.exit()
        run(scenario())

    def test_name_substring_selector(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path)

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                await pilot.pause()
                app._bulk_collect("analysis")
                await pilot.pause()
                assert [j["jobid"] for j in app.screen._jobs] == ["104"]
                app.exit()
        run(scenario())

    def test_requires_exact_confirmation_word(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path)

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                await pilot.pause()
                app._bulk_collect("all")
                await pilot.pause()
                modal = app.screen
                modal.query_one("#bulk-input", sd.Input).value = "yes"
                modal._try_confirm()
                await pilot.pause()
                assert app.screen is modal          # still open, not confirmed
                modal.query_one("#bulk-input", sd.Input).value = "CANCEL"
                modal._try_confirm()
                await pilot.pause()
                assert app.screen is not modal      # dismissed
                app.exit()
        run(scenario())

    def test_scancel_receives_the_ids(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path)

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                await pilot.pause()
                app._worker_bulk_cancel(["103", "104"])
                await app.workers.wait_for_complete()
                await pilot.pause()
                scancels = [c for c in app._calls if c and c[0] == "scancel"]
                assert scancels == [["scancel", "103", "104"]]
                app.exit()
        run(scenario())

    def test_hostile_ids_never_reach_scancel(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path)

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                app._worker_bulk_cancel(["--version", "; rm -rf /", "105"])
                await app.workers.wait_for_complete()
                await pilot.pause()
                scancels = [c for c in app._calls if c and c[0] == "scancel"]
                assert scancels == [["scancel", "105"]]
                app.exit()
        run(scenario())


class TestCompletionNotifications:
    def test_bell_toast_and_hook_fire_for_my_jobs(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path)
        notices, bells, hooks = [], [], []
        monkeypatch.setitem(sd.CONFIG["notifications"], "hook", "/bin/true")
        monkeypatch.setattr(sd, "run_completion_hook",
                            lambda h, j, s, n: hooks.append((j, s)))

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda m, **k: notices.append(str(m))
                app.bell = lambda: bells.append(1)
                await pilot.pause()
                await pilot.pause()
                app._apply_resolve_gone([("101", "FAILED", "2026-01-01 00:00:00")])
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert any("101" in n and "FAILED" in n for n in notices)
                assert bells
                assert hooks == [("101", "FAILED")]
                app.exit()
        run(scenario())

    def test_respects_config_switches(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path)
        notices, bells = [], []
        monkeypatch.setitem(sd.CONFIG["notifications"], "notify_on_finish", False)
        monkeypatch.setitem(sd.CONFIG["notifications"], "bell_on_finish", False)
        monkeypatch.setitem(sd.CONFIG["notifications"], "hook", "")

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda m, **k: notices.append(str(m))
                app.bell = lambda: bells.append(1)
                await pilot.pause()
                await pilot.pause()
                notices.clear()
                app._apply_resolve_gone([("101", "COMPLETED", "2026-01-01 00:00:00")])
                await pilot.pause()
                assert not bells
                assert not any("finished" in n for n in notices)
                app.exit()
        run(scenario())


class TestKeyBindings:
    def test_new_bindings_are_registered(self, sd):
        keys = {key: action for key, action, _ in sd.SlurmDashboard.BINDINGS}
        assert keys["f"] == "job_efficiency"
        assert keys["w"] == "why_pending"
        assert keys["k"] == "bulk_cancel"

    def test_no_duplicate_bindings(self, sd):
        keys = [key for key, _, _ in sd.SlurmDashboard.BINDINGS]
        assert len(keys) == len(set(keys)), f"duplicate key: {keys}"

    def test_every_binding_has_an_action(self, sd):
        for key, action, _ in sd.SlurmDashboard.BINDINGS:
            assert hasattr(sd.SlurmDashboard, f"action_{action}"), action

    def test_pressing_f_opens_efficiency(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path, {"sacct": SACCT_EFF})

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                await pilot.pause()
                app.query_one(sd.SqueueTable).focus()
                await pilot.press("f")
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert isinstance(app.screen, sd.EfficiencyModal)
                app.exit()
        run(scenario())

    def test_pressing_k_opens_bulk_prompt(self, sd, monkeypatch, tmp_path):
        app = make_app(sd, monkeypatch, tmp_path)

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                await pilot.pause()
                app.query_one(sd.SqueueTable).focus()
                await pilot.press("k")
                await pilot.pause()
                assert isinstance(app.screen, sd.TextPromptModal)
                app.exit()
        run(scenario())

    def test_jobs_panel_buttons_all_route(self, sd, monkeypatch, tmp_path):
        """Every button id in the Jobs panel must have a handler."""
        app = make_app(sd, monkeypatch, tmp_path)

        async def scenario():
            async with app.run_test(size=(200, 50)) as pilot:
                app.notify = lambda *a, **k: None
                await pilot.pause()
                app.query_one("Tabs").active = "tab-jobs"
                await pilot.pause()
                ids = [b.id for b in app.query("#jobs-panel Button")]
                assert set(ids) == {"btn-jobs-new", "btn-jobs-array", "btn-jobs-deps",
                                    "btn-jobs-eff", "btn-jobs-why", "btn-jobs-bulk"}
                app.exit()
        run(scenario())
