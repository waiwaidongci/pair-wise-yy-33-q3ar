import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, GridService, Store


class GridFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = GridService(Store(Path(self.tmp.name) / "g.db"))
        self.sub = self.s.register_asset("dispatcher", "dispatcher", "SUB", "中心站", "substation", 200, "A")
        self.line = self.s.register_asset("dispatcher", "dispatcher", "LINE", "线路", "line", 100, "A", self.sub["id"])
        self.s.register_facility("dispatcher", "dispatcher", "医院", "hospital", self.sub["id"], 1, 50)

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def plan(self, code="OUT-1"):
        outage = self.s.create_outage("dispatcher", "dispatcher", code, "线路跳闸", ["A"])
        plan = self.s.create_plan("dispatcher", "dispatcher", outage["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80, "critical": True},
            {"seq": 2, "action": "送电", "asset": "LINE", "required_mw": 70, "depends_on": [1], "critical": True}])
        plan = self.s.submit_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.approve_plan("dispatcher", "dispatcher", plan["id"], plan["revision"], "安全校核通过")
        return outage, self.s.activate_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])

    def test_full_restore_offline_merge_duplicate_and_plan_change(self):
        outage, plan = self.plan()
        report = self.s.field_report("field", "field", plan["id"], 1, "client-1", plan["version"], "completed", "设备已检查")
        self.assertEqual("merged", report["merge_status"])
        confirmed = self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed", "现场照片核验")
        self.assertEqual("confirmed", confirmed["status"])
        protected = self.s.field_report("field", "field", plan["id"], 1, "client-1-protected", plan["version"], "blocked", "补充遥测")
        self.assertEqual("protected", protected["merge_status"])
        plan2 = self.s.make_plan_change("dispatcher", "dispatcher", plan["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80, "critical": True},
            {"seq": 2, "action": "送电", "asset": "LINE", "required_mw": 70, "depends_on": [1], "critical": True}], plan["revision"])
        detail = self.s.plan_detail(plan2["id"])
        self.assertEqual(1, len(detail["confirmations"]))
        plan2 = self.s.submit_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.approve_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.activate_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        self.s.field_report("field", "field", plan2["id"], 2, "client-2", plan2["version"], "completed", "已送电")
        self.s.confirm_step("dispatcher", "dispatcher", plan2["id"], 2, "confirmed")
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan2["id"])
        self.assertEqual("restored", status["status"]["state"])

    def test_anomaly_stale_report_dependency_and_permissions(self):
        outage, plan = self.plan("OUT-2")
        anomaly = self.s.record_telemetry("operator", "operator", self.line["id"], 500, 220, "2026-09-24T00:00:00Z")
        self.assertFalse(anomaly["valid"])
        with self.assertRaises(ApiError):
            self.s.field_report("operator", "operator", plan["id"], 1, "bad-role", plan["version"], "completed")
        stale = self.s.field_report("field", "field", plan["id"], 2, "stale", plan["version"] - 1, "completed")
        self.assertEqual("conflict", stale["merge_status"])
        with self.assertRaises(ApiError):
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")
        with self.assertRaises(ApiError):
            self.s.create_plan("dispatcher", "dispatcher", outage["id"], [{"seq": 1, "action": "送电", "asset": "LINE", "required_mw": 101}])


    def test_field_correction_invalidates_confirmation_and_blocks_restore(self):
        outage, plan = self.plan("OUT-3")
        self.s.field_report("field", "field", plan["id"], 1, "c1", plan["version"], "completed")
        self.s.field_report("field", "field", plan["id"], 2, "c2", plan["version"], "completed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.assertEqual("restored", status["status"]["state"])
        corr = self.s.correct_field_report("field", "field", "c2", "c2-fix", plan["version"], "started", "设备未恢复，重新施工")
        self.assertEqual("merged", corr["merge_status"])
        detail = self.s.plan_detail(plan["id"])
        self.assertEqual(1, len(detail["invalid_confirmations"]))
        self.assertEqual(2, detail["invalid_confirmations"][0]["step_no"])
        self.assertTrue(detail["publish_blockers"])
        self.assertEqual(1, len(detail["correction_chains"]))
        chain = detail["correction_chains"][0]
        self.assertEqual(["c2", "c2-fix"], [v["client_report_id"] for v in chain["versions"]])
        self.assertEqual("c2-fix", chain["current_client_report_id"])
        self.assertEqual("restoring", self.s.state()["outages"][0]["state"])
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.assertEqual("restoring", status["status"]["state"])
        self.assertEqual(1, status["status"]["completed_steps"])
        self.assertEqual(1, status["status"]["invalid_confirmations"])
        with self.assertRaises(ApiError):
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")
        rep = self.s.field_report("field", "field", plan["id"], 2, "c3", plan["version"], "completed", "重新完成")
        self.assertEqual("merged", rep["merge_status"])
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.assertEqual("restored", status["status"]["state"])
        detail = self.s.plan_detail(plan["id"])
        self.assertEqual([], detail["invalid_confirmations"])
        archived = next(r for r in detail["field_reports"] if r["client_report_id"] == "c2")
        self.assertEqual(0, archived["is_current"])
        self.assertIsNotNone(archived["superseded_by"])

    def test_stale_correction_conflict_keeps_conclusion_and_chain(self):
        outage, plan = self.plan("OUT-4")
        self.s.field_report("field", "field", plan["id"], 1, "c1", plan["version"], "completed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        stale = self.s.correct_field_report("field", "field", "c1", "c1-fix", plan["version"] + 1, "started")
        self.assertEqual("conflict", stale["merge_status"])
        self.assertEqual("更正基于过期计划版本", stale["conflict_reason"])
        detail = self.s.plan_detail(plan["id"])
        self.assertEqual([], detail["invalid_confirmations"])
        self.assertEqual([], detail["publish_blockers"])
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.assertEqual(1, status["status"]["completed_steps"])
        again = self.s.correct_field_report("field", "field", "c1", "c1-fix", plan["version"] + 1, "started")
        self.assertEqual(stale["id"], again["id"])
        with self.assertRaises(ApiError):
            self.s.correct_field_report("dispatcher", "dispatcher", "c1", "c1-x", plan["version"], "started")
        with self.assertRaises(ApiError):
            self.s.correct_field_report("field", "field", "missing", "c1-y", plan["version"], "started")
        corr = self.s.correct_field_report("field", "field", "c1", "c1-fix2", plan["version"], "completed", "复核后确认完成")
        self.assertEqual("merged", corr["merge_status"])
        detail = self.s.plan_detail(plan["id"])
        chain = detail["correction_chains"][0]
        self.assertEqual(["c1", "c1-fix", "c1-fix2"], [v["client_report_id"] for v in chain["versions"]])
        self.assertEqual("c1-fix2", chain["current_client_report_id"])
        self.assertEqual(1, len(detail["invalid_confirmations"]))
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        self.assertEqual([], self.s.plan_detail(plan["id"])["invalid_confirmations"])


if __name__ == "__main__": unittest.main()
