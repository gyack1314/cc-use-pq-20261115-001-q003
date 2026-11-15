import csv
import io
import json
import zipfile

from autumn_ledger.demo import request_data, synthetic_event
from autumn_ledger.analysis.invariants import inspect_result
from autumn_ledger.analysis.narrative import explain_result
from autumn_ledger.demo import seed
from tests.support import LedgerCase


def scoped_edition(case):
    for city in ("sz", "nj"):
        for day in ("2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19",
                    "2026-11-20", "2026-11-21", "2026-11-22"):
            case.ledger.coverage.put({"source": "beta", "city": city, "day": day, "status": "complete"})
    for day in ("2026-11-21", "2026-11-22"):
        case.ledger.coverage.put({"source": "alpha", "city": "nj", "day": day,
                                  "status": "partial", "note": "周末数据未齐"}, 1)
    case.ledger.intake.ingest("beta", "beta-orders", [synthetic_event("c1", source="beta", day="2026-11-18")])
    return case.edition(sources=["alpha", "beta"])


class AdjustmentTests(LedgerCase):
    def adjustment(self, **changes):
        result = self.ledger.analysis.run(request_data(**changes))
        return result, result["comparison"]["composition_adjustment"]

    def layer(self, adjustment, source, city, day_type):
        return next(row for row in adjustment["layers"]
                    if (row["source"], row["city"], row["day_type"]) == (source, city, day_type))

    def test_stratified_exposure_and_intensity_per_layer(self):
        result, adjustment = self.adjustment()
        self.assertEqual(len(adjustment["layers"]), 4)
        self.assertTrue(all(row["class"] == "common" for row in adjustment["layers"]))
        sz_weekday = self.layer(adjustment, "alpha", "sz", "weekday")
        self.assertEqual(sz_weekday["periods"]["baseline"]["exposure"], 5)
        self.assertEqual(sz_weekday["periods"]["current"]["exposure"], 5)
        self.assertEqual(sz_weekday["periods"]["baseline"]["orders"], 1)
        self.assertEqual(sz_weekday["periods"]["current"]["orders"], 2)
        self.assertAlmostEqual(sz_weekday["periods"]["baseline"]["orders_per_exposure"], 0.2)
        self.assertAlmostEqual(sz_weekday["periods"]["current"]["orders_per_exposure"], 0.4)
        self.assertAlmostEqual(sz_weekday["periods"]["current"]["net_cents_per_exposure"], 20000)
        nj_weekend = self.layer(adjustment, "alpha", "nj", "weekend")
        self.assertEqual(nj_weekend["periods"]["baseline"]["exposure"], 2)
        self.assertAlmostEqual(nj_weekend["periods"]["baseline"]["net_cents_per_exposure"], 5000)
        self.assertTrue(result["statistical_checks"]["ok"])

    def test_decomposition_reconciles_with_frozen_baseline_weights(self):
        _, adjustment = self.adjustment()
        orders = adjustment["decomposition"]["orders"]
        self.assertEqual(orders["common_exposure"], {"baseline": 14, "current": 14})
        self.assertAlmostEqual(orders["intensity"]["baseline"], 2 / 14)
        self.assertAlmostEqual(orders["intensity"]["current"], 3 / 14)
        self.assertAlmostEqual(orders["intensity"]["current_at_baseline_weights"], 3 / 14)
        self.assertAlmostEqual(orders["raw_change"], 1 / 14)
        self.assertAlmostEqual(orders["components"]["within_layer"], 1 / 14)
        self.assertAlmostEqual(orders["components"]["composition"], 0)
        self.assertAlmostEqual(orders["components"]["interaction"], 0)
        net = adjustment["decomposition"]["net_cents"]
        self.assertAlmostEqual(net["components"]["within_layer"], 40000 / 7)
        self.assertAlmostEqual(net["raw_change"], 40000 / 7)
        for metric in ("orders", "net_cents"):
            content = adjustment["decomposition"][metric]
            components = content["components"]
            combined = components["within_layer"] + components["composition"] + components["interaction"]
            self.assertAlmostEqual(combined, content["raw_change"])
            self.assertTrue(content["reconciliation"]["ok"])
            for row in content["layers"]:
                self.assertIn("weight_baseline", row)
                self.assertIn("intensity_baseline", row)
        self.assertAlmostEqual(sum(row["weight_baseline"] for row in orders["layers"]), 1)

    def test_composition_shift_decomposes_and_marks_in_scope_gap(self):
        self.ledger.coverage.put({"source": "alpha", "city": "sz", "day": "2026-11-15",
                                  "status": "partial", "note": "未齐"}, 1)
        self.ledger.intake.ingest("alpha", "late", [synthetic_event("x1", day="2026-11-15")])
        result, adjustment = self.adjustment()
        orders = adjustment["decomposition"]["orders"]
        self.assertEqual(orders["common_exposure"], {"baseline": 13, "current": 14})
        self.assertAlmostEqual(orders["intensity"]["baseline"], 2 / 13)
        self.assertAlmostEqual(orders["intensity"]["current"], 3 / 14)
        self.assertAlmostEqual(orders["intensity"]["current_at_baseline_weights"], 3 / 13)
        self.assertAlmostEqual(orders["components"]["within_layer"], 1 / 13)
        self.assertAlmostEqual(orders["components"]["composition"], -1 / 91)
        self.assertAlmostEqual(orders["components"]["interaction"], -1 / 182)
        net = adjustment["decomposition"]["net_cents"]
        self.assertAlmostEqual(net["components"]["within_layer"], 80000 / 13)
        self.assertAlmostEqual(net["components"]["composition"], -20000 / 91)
        self.assertAlmostEqual(net["components"]["interaction"], -40000 / 91)
        reconciliation = adjustment["internal_reconciliation"]["baseline"]
        self.assertEqual(reconciliation["raw_totals"], {"orders": 3, "net_cents": 90000})
        self.assertEqual(reconciliation["in_scope_common_layers"], {"orders": 2, "net_cents": 40000})
        self.assertEqual(reconciliation["excluded_incomplete_coverage"], {"orders": 1, "net_cents": 50000})
        self.assertFalse(result["quality"]["scoped_publishable"])
        codes = {row["code"] for row in result["quality"]["issues"]}
        self.assertIn("scoped_coverage_incomplete", codes)
        self.assertFalse(result["quality"]["publishable"])
        self.assertTrue(result["statistical_checks"]["ok"])

    def test_entered_exited_layers_and_scoped_release_flow(self):
        edition = scoped_edition(self)
        stored = self.ledger.editions.get(edition["id"])
        adjustment = stored["result"]["comparison"]["composition_adjustment"]
        classes = {(row["source"], row["city"], row["day_type"]): row["class"] for row in adjustment["layers"]}
        self.assertEqual(classes[("alpha", "nj", "weekend")], "exited")
        self.assertEqual(classes[("beta", "sz", "weekday")], "entered")
        self.assertEqual(classes[("beta", "nj", "weekend")], "entered")
        self.assertEqual(sum(value == "common" for value in classes.values()), 3)
        exited = self.layer(adjustment, "alpha", "nj", "weekend")
        self.assertEqual(exited["periods"]["current"]["absence_reason"], "coverage_incomplete")
        self.assertIsNone(exited["periods"]["current"]["orders_per_exposure"])
        scope = adjustment["scope"]
        self.assertEqual(scope["common_exposure"], {"baseline": 12, "current": 12})
        self.assertEqual(len(scope["strata"]["entered"]), 4)
        self.assertEqual(len(scope["strata"]["exited"]), 1)
        self.assertTrue(scope["publishable"])
        discarded = scope["discarded"]
        self.assertEqual(discarded["orders_on_incomplete_cells"]["current"], 1)
        reconciliation = adjustment["internal_reconciliation"]
        self.assertEqual(reconciliation["current"]["raw_totals"]["orders"], 4)
        self.assertEqual(reconciliation["current"]["in_scope_common_layers"]["orders"], 2)
        self.assertEqual(reconciliation["current"]["out_of_scope_layers"]["orders"], 1)
        self.assertEqual(reconciliation["current"]["excluded_incomplete_coverage"]["orders"], 1)
        self.assertEqual(reconciliation["baseline"]["out_of_scope_layers"]["orders"], 1)
        quality = stored["result"]["quality"]
        self.assertFalse(quality["publishable"])
        self.assertTrue(quality["scoped_publishable"])
        digest = quality["scoped_option"]["digest"]
        self.assertEqual(digest, scope["digest"])
        error = self.assertError("quality_blocked", self.ledger.review.decide,
                                 edition["id"], "approve", 1, "reviewer", "说明")
        self.assertEqual(error.details["scoped_option"]["digest"], digest)
        self.assertError("scope_mismatch", self.ledger.review.decide,
                         edition["id"], "approve", 1, "reviewer", "说明", "0" * 64)
        approved = self.ledger.review.decide(edition["id"], "approve", 1, "reviewer",
                                             "已确认舍弃进入/退出层与缺报单元", digest)
        self.assertEqual(approved["state"], "approved")
        decisions = self.ledger.review.history(edition["id"])
        self.assertEqual(decisions[-1]["scope"], "common_layers")
        self.assertEqual(decisions[-1]["scope_digest"], digest)
        readiness = self.ledger.publication_readiness.inspect(edition["id"], "weekly")
        self.assertTrue(readiness["can_publish"])
        self.assertEqual(readiness["release_scope"], "common_layers")
        published = self.ledger.review.publish(edition["id"], "weekly", 2, 0, "publisher")
        self.assertEqual(published["generation"], 1)
        public = self.ledger.export.public_channel("weekly")
        self.assertEqual(public["result"]["comparison"]["composition_adjustment"]["scope"]["digest"], digest)
        publish_decision = self.ledger.review.history(edition["id"])[-1]
        self.assertEqual(publish_decision["action"], "publish")
        self.assertEqual(publish_decision["scope"], "common_layers")

    def test_scoped_approval_requires_fresh_confirmation_after_reopen(self):
        edition = scoped_edition(self)
        quality = self.ledger.editions.get(edition["id"])["result"]["quality"]
        digest = quality["scoped_option"]["digest"]
        self.ledger.review.decide(edition["id"], "approve", 1, "reviewer", "确认范围", digest)
        rejected = self.ledger.review.decide(edition["id"], "reject", 2, "reviewer", "补充说明")
        self.assertEqual(rejected["state"], "rejected")
        reopened = self.ledger.review.decide(edition["id"], "reopen", 3, "reviewer", "重新送审")
        self.assertError("quality_blocked", self.ledger.review.decide,
                         edition["id"], "approve", reopened["version"], "reviewer", "未带范围确认")
        approved = self.ledger.review.decide(edition["id"], "approve", reopened["version"],
                                             "reviewer", "再次确认范围", digest)
        self.assertEqual(approved["state"], "approved")

    def test_no_common_exposure_blocks_release_without_zero_fill(self):
        result = self.ledger.analysis.run(request_data(
            start="2026-11-16", end="2026-11-18",
            baseline_start="2026-11-14", baseline_end="2026-11-15"))
        comparison = result["comparison"]
        self.assertTrue(comparison["fully_comparable"])
        adjustment = comparison["composition_adjustment"]
        self.assertIsNone(adjustment["decomposition"])
        self.assertEqual(adjustment["decomposition_unavailable"], "no_common_exposure")
        classes = {row["day_type"]: row["class"] for row in adjustment["layers"] if row["city"] == "sz"}
        self.assertEqual(classes, {"weekday": "entered", "weekend": "exited"})
        entered = self.layer(adjustment, "alpha", "sz", "weekday")
        self.assertEqual(entered["periods"]["baseline"]["absence_reason"], "no_window_dates")
        self.assertIsNone(entered["periods"]["baseline"]["orders_per_exposure"])
        self.assertIsNone(entered["periods"]["baseline"]["net_cents_per_exposure"])
        exited = self.layer(adjustment, "alpha", "sz", "weekend")
        self.assertEqual(exited["periods"]["current"]["absence_reason"], "no_window_dates")
        self.assertFalse(adjustment["scope"]["publishable"])
        self.assertEqual(adjustment["scope"]["blockers"], ["no_common_exposure"])
        codes = {row["code"] for row in result["quality"]["issues"]}
        self.assertIn("no_common_exposure", codes)
        self.assertFalse(result["quality"]["publishable"])
        self.assertFalse(result["quality"]["scoped_publishable"])
        self.assertTrue(result["statistical_checks"]["ok"])
        edition = self.ledger.editions.create("no-common", request_data(
            start="2026-11-16", end="2026-11-18",
            baseline_start="2026-11-14", baseline_end="2026-11-15"))
        self.assertError("quality_blocked", self.ledger.review.decide,
                         edition["id"], "approve", 1, "reviewer", "说明")

    def test_unobserved_layer_is_not_zero_and_backfill_does_not_drift(self):
        edition = self.edition(sources=["alpha", "beta"])
        stored = self.ledger.editions.get(edition["id"])
        adjustment = stored["result"]["comparison"]["composition_adjustment"]
        unobserved = self.layer(adjustment, "beta", "sz", "weekday")
        self.assertEqual(unobserved["class"], "unobserved")
        for name in ("baseline", "current"):
            self.assertEqual(unobserved["periods"][name]["absence_reason"], "coverage_incomplete")
            self.assertIsNone(unobserved["periods"][name]["orders_per_exposure"])
        self.assertEqual(len(adjustment["scope"]["strata"]["unobserved"]), 4)
        self.assertTrue(stored["result"]["quality"]["scoped_publishable"])
        digest = stored["result"]["quality"]["scoped_option"]["digest"]
        approved = self.ledger.review.decide(edition["id"], "approve", 1, "reviewer", "确认舍弃beta层", digest)
        self.ledger.review.publish(edition["id"], "weekly", approved["version"], 0, "publisher")
        before = self.ledger.export.document(edition["id"])
        from autumn_ledger.values import date_range
        for city in ("sz", "nj"):
            for day in date_range("2026-11-09", "2026-11-22"):
                self.ledger.coverage.put({"source": "beta", "city": city,
                                          "day": day.isoformat(), "status": "complete"})
        stale = self.ledger.editions.get(edition["id"])
        self.assertTrue(stale["stale"])
        self.assertEqual(self.ledger.export.document(edition["id"]), before)
        self.assertTrue(self.ledger.editions.verify(edition["id"])["ok"])
        replacement = self.ledger.editions.create("backfilled", request_data(sources=["alpha", "beta"]))
        new_adjustment = self.ledger.editions.get(replacement["id"])["result"]["comparison"]["composition_adjustment"]
        self.assertEqual(len(new_adjustment["scope"]["strata"]["common"]), 8)
        self.assertEqual(new_adjustment["scope"]["strata"]["unobserved"], [])
        self.assertNotEqual(new_adjustment["scope"]["digest"], digest)
        old_adjustment = self.ledger.editions.get(edition["id"])["result"]["comparison"]["composition_adjustment"]
        self.assertEqual(old_adjustment, adjustment)

    def test_adjustment_saved_with_edition_and_exported(self):
        edition = self.edition()
        identity = edition["id"]
        stored = self.ledger.editions.get(identity)["result"]
        adjustment = stored["comparison"]["composition_adjustment"]
        self.assertEqual(adjustment["stratification"]["dimensions"], ["source", "city", "day_type"])
        weights = {row["city"] + ":" + row["day_type"]: row["weight_baseline"]
                   for row in adjustment["decomposition"]["orders"]["layers"]}
        self.assertAlmostEqual(weights["sz:weekday"], 5 / 14)
        explanation = self.ledger.editions.explain(identity)
        self.assertEqual(explanation["adjustment_scope"]["digest"], adjustment["scope"]["digest"])
        self.assertEqual(explanation["adjustment_scope"]["strata"], {"common": 4, "entered": 0,
                                                                     "exited": 0, "unobserved": 0})
        self.assertTrue(explanation["adjustment_scope"]["decomposition_available"])
        content = self.ledger.export.adjustment(identity)
        rows = {row["city"] + ":" + row["day_type"]: row
                for row in csv.DictReader(io.StringIO(content))}
        self.assertEqual(len(rows), 4)
        sz_weekday = rows["sz:weekday"]
        self.assertEqual(sz_weekday["layer_class"], "common")
        self.assertEqual(sz_weekday["baseline_exposure"], "5")
        self.assertEqual(sz_weekday["current_orders"], "2")
        self.assertAlmostEqual(float(sz_weekday["weight_baseline"]), 5 / 14)
        self.assertAlmostEqual(float(sz_weekday["within_layer_orders"]), 1 / 14)
        bundle = self.ledger.export.bundle(identity)
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertIn("comparison/adjustment.csv", archive.namelist())
            document = json.loads(archive.read("edition.json"))
        self.assertTrue(self.ledger.export.verify_bundle(bundle)["ok"])
        saved = document["result"]["comparison"]["composition_adjustment"]
        self.assertEqual(saved["scope"]["digest"], adjustment["scope"]["digest"])
        narrative = explain_result(stored)
        codes = {row["code"] for row in narrative["observations"]}
        self.assertIn("composition_adjustment", codes)
        self.assertTrue(self.ledger.editions.verify(identity)["ok"])

    def test_tampered_decomposition_fails_statistical_checks(self):
        result = self.ledger.analysis.run(request_data())
        result["comparison"]["composition_adjustment"]["decomposition"]["orders"]["components"]["within_layer"] += 1
        outcome = inspect_result(result)
        self.assertFalse(outcome["ok"])
        names = {row["name"] for row in outcome["failures"]}
        self.assertIn("adjustment:orders:identity", names)
        self.assertIn("adjustment:orders:layers:within_layer", names)

    def test_full_release_does_not_accept_scope_parameter(self):
        edition = self.edition()
        quality = self.ledger.editions.get(edition["id"])["result"]["quality"]
        self.assertTrue(quality["publishable"])
        digest = quality["scoped_option"]["digest"]
        self.assertError("unexpected_scope", self.ledger.review.decide,
                         edition["id"], "approve", 1, "reviewer", "说明", digest)
        approved = self.approve(edition)
        self.assertEqual(approved["state"], "approved")
        decision = self.ledger.review.history(edition["id"])[-1]
        self.assertEqual(decision["scope"], "full")
        self.assertIsNone(decision["scope_digest"])

    def test_scope_acknowledgment_flows_through_http_api(self):
        from fastapi.testclient import TestClient
        from autumn_ledger.api.server import create_app
        app = create_app(self.path)
        with TestClient(app) as client:
            for city in ("sz", "nj"):
                for day in ("2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19",
                            "2026-11-20", "2026-11-21", "2026-11-22"):
                    client.put("/coverage", json={"value": {"source": "beta", "city": city, "day": day,
                                                            "status": "complete"}})
            created = client.post("/editions", json={"request_key": "http-scoped",
                                                     "request": request_data(sources=["alpha", "beta"])})
            self.assertEqual(created.status_code, 200, created.text)
            identity = created.json()["id"]
            blocked = client.post(f"/editions/{identity}/decision",
                                  json={"action": "approve", "expected_version": 1,
                                        "actor": "reviewer", "note": "未确认范围"})
            self.assertEqual(blocked.status_code, 409)
            self.assertEqual(blocked.json()["error"]["code"], "quality_blocked")
            digest = blocked.json()["error"]["details"]["scoped_option"]["digest"]
            approved = client.post(f"/editions/{identity}/decision",
                                   json={"action": "approve", "expected_version": 1, "actor": "reviewer",
                                         "note": "确认共同层范围", "accepted_scope": digest})
            self.assertEqual(approved.status_code, 200, approved.text)
            published = client.post(f"/editions/{identity}/publish",
                                    json={"channel": "weekly", "expected_version": 2,
                                          "expected_generation": 0, "actor": "publisher"})
            self.assertEqual(published.status_code, 200, published.text)
            public = client.get("/public/weekly")
            self.assertEqual(public.status_code, 200)
            adjustment = public.json()["result"]["comparison"]["composition_adjustment"]
            self.assertEqual(adjustment["scope"]["digest"], digest)
            export = client.get(f"/editions/{identity}/adjustment.csv")
            self.assertEqual(export.status_code, 200)
            self.assertIn("text/csv", export.headers["content-type"])
