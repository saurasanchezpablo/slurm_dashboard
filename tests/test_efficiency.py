"""seff-style efficiency: measured usage versus what was reserved."""
import pytest

# Allocation row carries Elapsed/NCPUS/ReqMem/AllocTRES; MaxRSS lives on the
# step rows, which is why sacct is queried without -X.
ONE_HOUR_4CPU = "\n".join([
    "1001|COMPLETED|01:00:00|03:30:00|4|1|16G||cpu=4,mem=16G,node=1,gres/gpu=2|0:0",
    "1001.batch|COMPLETED|01:00:00|03:29:00|4|1||2048M||0:0",
    "1001.extern|COMPLETED|01:00:00|00:00:01|4|1||1024K||0:0",
])


@pytest.fixture
def rec(sd, fake_slurm):
    fake_slurm.set("sacct", ONE_HOUR_4CPU)
    return sd.get_job_efficiency(["1001"])["1001"]


class TestPerJob:
    def test_cpu_efficiency(self, rec):
        assert rec["cpu_eff"] == 87.5          # 3.5 CPU-h of 4 reserved

    def test_memory_peak_taken_from_steps(self, rec):
        assert rec["max_rss_mb"] == 2048.0

    def test_memory_efficiency(self, rec):
        assert rec["mem_eff"] == 12.5          # 2 GB of 16 GB

    def test_billed_resources(self, rec):
        assert rec["cpu_hours"] == 4.0
        assert rec["gpu_hours"] == 2.0
        assert rec["gpus"] == 2

    def test_unused_memory_is_reported(self, rec):
        assert rec["wasted_mem_mb"] == 14336.0

    def test_verdict(self, sd, rec):
        assert sd.efficiency_verdict(rec)[0] == "over-allocated"

    def test_suggests_a_smaller_request(self, sd, rec):
        tips = " ".join(sd.efficiency_suggestions(rec))
        assert "--mem=3G" in tips

    def test_per_cpu_reqmem_is_scaled(self, sd, fake_slurm):
        fake_slurm.set("sacct", "\n".join([
            "2|COMPLETED|01:00:00|04:00:00|4|1|4Gc||cpu=4|0:0",
            "2.batch|COMPLETED|01:00:00|04:00:00|4|1||8192M||0:0",
        ]))
        r = sd.get_job_efficiency(["2"])["2"]
        assert r["req_mem_mb"] == 16384.0      # 4G per CPU x 4 CPUs
        assert r["mem_eff"] == 50.0
        assert r["cpu_eff"] == 100.0

    def test_per_node_reqmem_is_scaled(self, sd, fake_slurm):
        fake_slurm.set("sacct", "3|COMPLETED|01:00:00|02:00:00|2|2|8Gn||cpu=2|0:0")
        assert sd.get_job_efficiency(["3"])["3"]["req_mem_mb"] == 16384.0

    def test_zero_elapsed_gives_no_efficiency(self, sd, fake_slurm):
        fake_slurm.set("sacct", "4|FAILED|00:00:00|00:00:00|4|1|16G||cpu=4|1:0")
        r = sd.get_job_efficiency(["4"])["4"]
        assert r["cpu_eff"] is None
        assert sd.efficiency_verdict(r)[0] in ("no data", "over-allocated")

    def test_unknown_job_absent(self, sd, fake_slurm):
        fake_slurm.set("sacct", "")
        assert sd.get_job_efficiency(["999"]) == {}

    def test_invalid_ids_filtered(self, sd, fake_slurm):
        fake_slurm.set("sacct", "")
        sd.get_job_efficiency(["--evil"])
        assert fake_slurm.calls == []

    def test_timeout_advice(self, sd):
        tips = sd.efficiency_suggestions(
            {"cpu_eff": 90.0, "mem_eff": 50.0, "ncpus": 1,
             "req_mem_mb": 1000.0, "max_rss_mb": 500.0, "state": "TIMEOUT"})
        assert any("--time" in t for t in tips)


class TestAggregate:
    def test_rolls_up_waste(self, sd, fake_slurm):
        fake_slurm.set("sacct", "\n".join([
            "1|COMPLETED|01:00:00|04:00:00|4|1|4G||cpu=4|0:0",     # 100% cpu
            "1.batch|COMPLETED|01:00:00|04:00:00|4|1||4096M||0:0",
            "2|COMPLETED|01:00:00|00:24:00|4|1|16G||cpu=4|0:0",    # 10% cpu
            "2.batch|COMPLETED|01:00:00|00:24:00|4|1||512M||0:0",
        ]))
        agg = sd.aggregate_efficiency(sd.get_job_efficiency(["1", "2"]))
        assert agg["jobs"] == 2
        assert agg["core_hours"] == 8.0
        assert agg["wasted_core_hours"] == pytest.approx(3.6, abs=0.05)
        assert agg["mean_cpu_eff"] == pytest.approx(55.0, abs=0.1)
        assert agg["buckets"]["0-25%"] == 1
        assert agg["buckets"]["75-100%"] == 1
        assert agg["worst"][0]["jobid"] == "2"   # biggest waste first

    def test_empty_input(self, sd):
        assert sd.aggregate_efficiency({}) == {}
