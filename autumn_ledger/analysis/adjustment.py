from ..records.contracts import contribution
from ..values import date_range, fingerprint, safe_ratio
from .windows import day_type


DAY_TYPES = ("weekday", "weekend")
ADJUSTED_METRICS = ("orders", "net_cents")
INTENSITY_KEYS = {"orders": "orders_per_exposure", "net_cents": "net_cents_per_exposure"}


def close(left, right, tolerance=1e-9):
    return abs(left - right) <= tolerance * max(1.0, abs(left), abs(right))


def period_strata(period, sources, cities):
    window = period["window"]
    typed_days = {kind: [] for kind in DAY_TYPES}
    for day in date_range(window["start"], window["end"]):
        typed_days[day_type(day.isoformat())].append(day.isoformat())
    statuses = {}
    for row in period["coverage"]:
        statuses[(row["source"], row["city"], row["day"])] = row["status"]
    strata = {}
    for source in sorted(sources):
        for city in sorted(cities):
            for kind in DAY_TYPES:
                counts = {"complete": 0, "partial": 0, "missing": 0}
                complete_days = []
                for day in typed_days[kind]:
                    status = statuses.get((source, city, day), "missing")
                    counts[status] += 1
                    if status == "complete":
                        complete_days.append(day)
                strata[(source, city, kind)] = {"expected_dates": len(typed_days[kind]),
                                                "complete_dates": counts["complete"],
                                                "partial_dates": counts["partial"],
                                                "missing_dates": counts["missing"],
                                                "exposure": counts["complete"],
                                                "complete_days": complete_days,
                                                "orders": 0, "net_cents": 0}
    complete_cells = {(source, city, day) for (source, city, _kind), cell in strata.items()
                      for day in cell["complete_days"]}
    excluded = {}
    for order in period["orders"]:
        measures = contribution(order)
        cell = (order["source"], order["origin"], order["travel_day"])
        if cell in complete_cells:
            layer = strata[(order["source"], order["origin"], day_type(order["travel_day"]))]
            layer["orders"] += measures["orders"]
            layer["net_cents"] += measures["net_cents"]
        else:
            key = (order["source"], order["origin"], day_type(order["travel_day"]))
            bucket = excluded.setdefault(key, {"orders": 0, "net_cents": 0})
            bucket["orders"] += measures["orders"]
            bucket["net_cents"] += measures["net_cents"]
    return strata, excluded


def layer_record(source, city, kind, current, baseline):
    record = {"source": source, "city": city, "day_type": kind, "periods": {}}
    for name, cell in (("baseline", baseline), ("current", current)):
        exposure = cell["exposure"]
        record["periods"][name] = {
            "expected_dates": cell["expected_dates"], "complete_dates": cell["complete_dates"],
            "partial_dates": cell["partial_dates"], "missing_dates": cell["missing_dates"],
            "exposure": exposure, "orders": cell["orders"], "net_cents": cell["net_cents"],
            "orders_per_exposure": safe_ratio(cell["orders"], exposure),
            "net_cents_per_exposure": safe_ratio(cell["net_cents"], exposure),
            "absence_reason": None if exposure else (
                "no_window_dates" if cell["expected_dates"] == 0 else "coverage_incomplete")}
    current_exposure = record["periods"]["current"]["exposure"]
    baseline_exposure = record["periods"]["baseline"]["exposure"]
    if current_exposure and baseline_exposure:
        record["class"] = "common"
    elif current_exposure:
        record["class"] = "entered"
    elif baseline_exposure:
        record["class"] = "exited"
    else:
        record["class"] = "unobserved"
    return record


def decompose(layers, metric):
    common = [layer for layer in layers if layer["class"] == "common"]
    baseline_exposure = sum(layer["periods"]["baseline"]["exposure"] for layer in common)
    current_exposure = sum(layer["periods"]["current"]["exposure"] for layer in common)
    if not baseline_exposure or not current_exposure:
        return None
    key = INTENSITY_KEYS[metric]
    rows = []
    for layer in common:
        baseline_cell = layer["periods"]["baseline"]
        current_cell = layer["periods"]["current"]
        weight_baseline = baseline_cell["exposure"] / baseline_exposure
        weight_current = current_cell["exposure"] / current_exposure
        intensity_baseline = baseline_cell[key]
        intensity_current = current_cell[key]
        rows.append({"source": layer["source"], "city": layer["city"], "day_type": layer["day_type"],
                     "exposure_baseline": baseline_cell["exposure"], "exposure_current": current_cell["exposure"],
                     "weight_baseline": weight_baseline, "weight_current": weight_current,
                     "intensity_baseline": intensity_baseline, "intensity_current": intensity_current,
                     "within_layer": weight_baseline * (intensity_current - intensity_baseline),
                     "composition": (weight_current - weight_baseline) * intensity_baseline,
                     "interaction": (weight_current - weight_baseline) * (intensity_current - intensity_baseline)})
    within = sum(row["within_layer"] for row in rows)
    composition = sum(row["composition"] for row in rows)
    interaction = sum(row["interaction"] for row in rows)
    intensity_baseline = sum(row["weight_baseline"] * row["intensity_baseline"] for row in rows)
    intensity_current = sum(row["weight_current"] * row["intensity_current"] for row in rows)
    adjusted_current = sum(row["weight_baseline"] * row["intensity_current"] for row in rows)
    raw_change = intensity_current - intensity_baseline
    checks = [
        {"name": "identity", "ok": close(within + composition + interaction, raw_change),
         "expected": raw_change, "actual": within + composition + interaction},
        {"name": "frozen_weights", "ok": close(adjusted_current - intensity_baseline, within),
         "expected": within, "actual": adjusted_current - intensity_baseline},
        {"name": "weights_baseline", "ok": close(sum(row["weight_baseline"] for row in rows), 1.0),
         "expected": 1.0, "actual": sum(row["weight_baseline"] for row in rows)},
        {"name": "weights_current", "ok": close(sum(row["weight_current"] for row in rows), 1.0),
         "expected": 1.0, "actual": sum(row["weight_current"] for row in rows)},
        {"name": "pooled_baseline", "ok": close(intensity_baseline,
                                                sum(layer["periods"]["baseline"][metric] for layer in common) / baseline_exposure),
         "expected": sum(layer["periods"]["baseline"][metric] for layer in common) / baseline_exposure,
         "actual": intensity_baseline},
        {"name": "pooled_current", "ok": close(intensity_current,
                                               sum(layer["periods"]["current"][metric] for layer in common) / current_exposure),
         "expected": sum(layer["periods"]["current"][metric] for layer in common) / current_exposure,
         "actual": intensity_current}]
    return {"metric": metric,
            "common_exposure": {"baseline": baseline_exposure, "current": current_exposure},
            "intensity": {"baseline": intensity_baseline, "current": intensity_current,
                          "current_at_baseline_weights": adjusted_current},
            "raw_change": raw_change,
            "components": {"within_layer": within, "composition": composition, "interaction": interaction},
            "layers": rows,
            "reconciliation": {"ok": all(item["ok"] for item in checks), "checks": checks}}


def layer_key(layer):
    return {"source": layer["source"], "city": layer["city"], "day_type": layer["day_type"]}


def scope_block(layers, excluded):
    grouped = {"common": [], "entered": [], "exited": [], "unobserved": []}
    for layer in layers:
        grouped[layer["class"]].append(layer)
    in_scope_cells = {"baseline": 0, "current": 0}
    in_scope_layers = []
    for layer in grouped["common"]:
        counts = {}
        for name in ("baseline", "current"):
            cell = layer["periods"][name]
            incomplete = cell["partial_dates"] + cell["missing_dates"]
            counts[name] = incomplete
            in_scope_cells[name] += incomplete
        if counts["baseline"] or counts["current"]:
            in_scope_layers.append({**layer_key(layer), "incomplete_dates": counts})
    discarded_layers = []
    for name in ("entered", "exited", "unobserved"):
        for layer in grouped[name]:
            discarded_layers.append({**layer_key(layer), "class": name,
                                     "baseline": {"exposure": layer["periods"]["baseline"]["exposure"],
                                                  "orders": layer["periods"]["baseline"]["orders"],
                                                  "net_cents": layer["periods"]["baseline"]["net_cents"]},
                                     "current": {"exposure": layer["periods"]["current"]["exposure"],
                                                 "orders": layer["periods"]["current"]["orders"],
                                                 "net_cents": layer["periods"]["current"]["net_cents"]}})
    incomplete_cells = {"baseline": 0, "current": 0}
    for layer in grouped["entered"] + grouped["exited"] + grouped["unobserved"]:
        for name in ("baseline", "current"):
            cell = layer["periods"][name]
            incomplete_cells[name] += cell["partial_dates"] + cell["missing_dates"]
    excluded_outside = {"baseline": {"orders": 0, "net_cents": 0}, "current": {"orders": 0, "net_cents": 0}}
    excluded_inside = {"baseline": {"orders": 0, "net_cents": 0}, "current": {"orders": 0, "net_cents": 0}}
    common_keys = {(layer["source"], layer["city"], layer["day_type"]) for layer in grouped["common"]}
    for name in ("baseline", "current"):
        for key, measures in excluded[name].items():
            target = excluded_inside if key in common_keys else excluded_outside
            target[name]["orders"] += measures["orders"]
            target[name]["net_cents"] += measures["net_cents"]
    core = {"kind": "common_layers",
            "strata": {name: [layer_key(layer) for layer in grouped[name]] for name in grouped},
            "common_exposure": {
                "baseline": sum(layer["periods"]["baseline"]["exposure"] for layer in grouped["common"]),
                "current": sum(layer["periods"]["current"]["exposure"] for layer in grouped["common"])},
            "in_scope_incomplete": {"cells": in_scope_cells, "layers": in_scope_layers},
            "discarded": {"layers": discarded_layers,
                          "incomplete_cells": incomplete_cells,
                          "orders_on_incomplete_cells": {name: excluded_outside[name]["orders"] for name in excluded_outside},
                          "net_cents_on_incomplete_cells": {name: excluded_outside[name]["net_cents"] for name in excluded_outside}}}
    blockers = []
    if not core["common_exposure"]["baseline"] or not core["common_exposure"]["current"]:
        blockers.append("no_common_exposure")
    if in_scope_cells["baseline"] or in_scope_cells["current"]:
        blockers.append("scoped_coverage_incomplete")
    return {**core, "publishable": not blockers, "blockers": blockers, "digest": fingerprint(core)}


def internal_reconciliation(layers, excluded, period_totals):
    result = {}
    common = [layer for layer in layers if layer["class"] == "common"]
    outside = [layer for layer in layers if layer["class"] != "common"]
    for name in ("baseline", "current"):
        in_scope = {"orders": sum(layer["periods"][name]["orders"] for layer in common),
                    "net_cents": sum(layer["periods"][name]["net_cents"] for layer in common)}
        out_of_scope = {"orders": sum(layer["periods"][name]["orders"] for layer in outside),
                        "net_cents": sum(layer["periods"][name]["net_cents"] for layer in outside)}
        incomplete = {"orders": sum(cell["orders"] for cell in excluded[name].values()),
                      "net_cents": sum(cell["net_cents"] for cell in excluded[name].values())}
        result[name] = {"raw_totals": {"orders": period_totals[name]["orders"],
                                       "net_cents": period_totals[name]["net_cents"]},
                        "in_scope_common_layers": in_scope,
                        "out_of_scope_layers": out_of_scope,
                        "excluded_incomplete_coverage": incomplete}
    result["note"] = "范围外原始总量仅用于内部核对，不进入限于共同层的公开比较"
    return result


def composition_adjustment(current_period, baseline_period, request):
    sources, cities = request["sources"], request["cities"]
    current_strata, current_excluded = period_strata(current_period, sources, cities)
    baseline_strata, baseline_excluded = period_strata(baseline_period, sources, cities)
    layers = [layer_record(source, city, kind, current_strata[(source, city, kind)],
                           baseline_strata[(source, city, kind)])
              for source in sorted(sources) for city in sorted(cities) for kind in DAY_TYPES]
    decompositions = {metric: decompose(layers, metric) for metric in ADJUSTED_METRICS}
    decompositions = {metric: value for metric, value in decompositions.items() if value is not None}
    scope = scope_block(layers, {"baseline": baseline_excluded, "current": current_excluded})
    totals = {"baseline": baseline_period["cube"]["totals"], "current": current_period["cube"]["totals"]}
    return {"stratification": {"dimensions": ["source", "city", "day_type"],
                               "exposure": "已确认完整(complete)的来源城市日期数",
                               "metrics": list(ADJUSTED_METRICS),
                               "numerator": "仅统计已确认完整单元上的订单，缺报或部分覆盖单元不同时计入分子与分母"},
            "layers": layers,
            "decomposition": decompositions or None,
            "decomposition_unavailable": None if decompositions else "no_common_exposure",
            "scope": scope,
            "internal_reconciliation": internal_reconciliation(layers,
                                                               {"baseline": baseline_excluded, "current": current_excluded},
                                                               totals),
            "notes": ["缺报或部分覆盖不记为零：相关来源城市日期从暴露与分子同时剔除，仅列入内部核对",
                      "某期没有该类日期的层不参与强度比较，也不视为零强度",
                      "分解仅限两期都有暴露的共同层；进入与退出层的原始量仅列入内部核对",
                      "构成调整仅描述所选来源样本，不能用于证明校历政策的因果效果"]}
