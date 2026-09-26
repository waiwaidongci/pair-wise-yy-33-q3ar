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

    def test_correction_invalidates_confirmation_and_blocks_completion(self):
        outage, plan = self.plan("OUT-3")
        self.s.field_report("field", "field", plan["id"], 1, "c-1", plan["version"], "completed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        correction = self.s.field_report("field", "field", plan["id"], 1, "c-1", plan["version"], "started", "设备仍未恢复", report_version=2)
        self.assertEqual("merged", correction["merge_status"])
        self.assertEqual(2, correction["report_version"])
        self.assertIsNotNone(correction["invalidated_confirmation_id"])
        detail = self.s.plan_detail(plan["id"])
        chain = next(c for c in detail["correction_chains"] if c["client_report_id"] == "c-1")
        self.assertEqual(2, chain["current_version"])
        self.assertEqual("archived", chain["versions"][0]["record_state"])
        self.assertEqual("current", chain["versions"][1]["record_state"])
        confirmation = next(c for c in detail["confirmations"] if c["step_no"] == 1)
        self.assertEqual("invalidated", confirmation["state"])
        self.assertEqual(1, len(detail["blocking_reasons"]))
        self.s.field_report("field", "field", plan["id"], 2, "c-2", plan["version"], "completed")
        with self.assertRaises(ApiError):
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.assertEqual("restoring", status["status"]["state"])
        self.assertEqual(0, status["status"]["completed_steps"])
        self.assertEqual(1, status["status"]["invalidated_confirmations"])
        with self.assertRaises(ApiError):
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        self.s.field_report("field", "field", plan["id"], 1, "c-1", plan["version"], "completed", "复查通过", report_version=3)
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.assertEqual("restored", status["status"]["state"])
        self.assertEqual(0, status["status"]["invalidated_confirmations"])

    def test_correction_with_stale_plan_version_conflicts_without_changing_conclusion(self):
        outage, plan = self.plan("OUT-4")
        self.s.field_report("field", "field", plan["id"], 1, "c-1", plan["version"], "completed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        stale = self.s.field_report("field", "field", plan["id"], 1, "c-1", plan["version"] + 1, "started", "误报撤回", report_version=2)
        self.assertEqual("conflict", stale["merge_status"])
        self.assertEqual("archived", stale["record_state"])
        detail = self.s.plan_detail(plan["id"])
        confirmation = next(c for c in detail["confirmations"] if c["step_no"] == 1)
        self.assertEqual("active", confirmation["state"])
        chain = next(c for c in detail["correction_chains"] if c["client_report_id"] == "c-1")
        self.assertEqual(1, chain["current_version"])
        self.assertEqual("current", chain["versions"][0]["record_state"])
        self.assertEqual("archived", chain["versions"][1]["record_state"])
        self.assertEqual([], detail["blocking_reasons"])
        replay = self.s.field_report("field", "field", plan["id"], 1, "c-1", plan["version"], "completed")
        self.assertEqual(1, replay["report_version"])
        jumped = self.s.field_report("field", "field", plan["id"], 1, "c-1", plan["version"], "completed", report_version=4)
        self.assertEqual("merged", jumped["merge_status"])
        with self.assertRaises(ApiError):
            self.s.field_report("field", "field", plan["id"], 1, "c-1", plan["version"], "completed", report_version=3)


if __name__ == "__main__": unittest.main()
