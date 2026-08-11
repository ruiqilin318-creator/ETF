from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "option_report_data.json"
TARGET = ROOT / "data" / "dashboard_data.json"
SYMBOL_ORDER = ["510050", "510300", "510500", "159915", "588000"]


def build_strategy(raw: dict[str, Any], code: str, direction: str) -> dict[str, Any] | None:
    selection = raw["selections"][code].get(direction)
    if not selection:
        return None

    options = raw["options"][code]
    by_id = {option["security_id"]: option for option in options}
    main = by_id[selection["main_id"]]
    insurance = by_id[selection["insurance_id"]]
    spot = raw["spots"][code]["last"]

    def dte(expiry: str) -> int:
        from datetime import date

        return (date.fromisoformat(expiry) - date.fromisoformat(raw["asof_date"])).days

    def moneyness(option: dict[str, Any]) -> dict[str, Any]:
        distance = abs(option["strike"] / spot - 1) * 100
        if distance <= 0.6:
            label = "平值附近"
        else:
            is_itm = option["strike"] < spot if option["cp"] == "C" else option["strike"] > spot
            label = "轻度实值" if is_itm else "轻度虚值"
        return {"label": label, "distance": distance}

    def leg(option: dict[str, Any], quantity: int, role: str) -> dict[str, Any]:
        return {
            "id": option["security_id"],
            "contract": option["contract_id"],
            "name": option["name"],
            "role": role,
            "quantity": quantity,
            "expiry": option["expiry"],
            "strike": option["strike"],
            "unit": option["unit"],
            "bid": option["bid"],
            "ask": option["ask"],
            "bidQty": option["bid_qty"],
            "askQty": option["ask_qty"],
            "volume": option["volume"],
            "oi": option["oi"],
            "iv": option["iv"] * 100,
            "delta": option["delta"],
            "gamma": option["gamma"],
            "quoteTime": option.get("official_time") or option["quote_date"],
            "moneyness": moneyness(option),
            "dte": dte(option["expiry"]),
        }

    scenario = selection["scenario"]
    return {
        "direction": direction,
        "isFallback": selection.get("is_fallback", False),
        "fallbackReason": selection.get("fallback_reason"),
        "score": selection["score"],
        "cost": selection["metrics"]["cost"],
        "mainShare": selection["metrics"]["main_share"] * 100,
        "coverage": selection["metrics"]["coverage"] * 100,
        "liquidity": selection["metrics"]["liquidity"],
        "roundtrip": scenario["roundtrip_now"],
        "greeks": selection["greeks"],
        "sourceGreeks": selection["source_greeks"],
        "greekReconciliation": selection["greek_reconciliation"],
        "legs": [
            leg(main, selection["n_main"], "方向主腿"),
            leg(insurance, selection["n_ins"], "反向保险"),
        ],
        "scenarios": scenario["table"],
        "ivStress": scenario["iv_stress"],
        "worstT5Move": scenario["worst_t5_move"] * 100,
        "worstT5Pnl": scenario["worst_t5_pnl"],
        "breakevens": [value * 100 for value in scenario["expiry_breakevens"]],
        "breakevenStatus": scenario["breakeven_status"],
        "breakevenNote": scenario["breakeven_note"],
        "maxRisk": scenario["max_premium_risk"],
        "alternatives": [
            {
                "label": (
                    f"{item['n_main']}×{by_id[item['main_id']]['name']} + "
                    f"{item['n_ins']}×{by_id[item['insurance_id']]['name']}"
                ),
                "score": item["score"],
                "cost": item["metrics"]["cost"],
            }
            for item in selection.get("alternatives", [])[:2]
        ],
    }


def build_dashboard(raw: dict[str, Any]) -> dict[str, Any]:
    instruments = []
    for code in SYMBOL_ORDER:
        spot = raw["spots"][code]
        indicator = raw["indicators"][code]
        selection = raw["selections"][code]
        instruments.append(
            {
                "code": code,
                "name": spot["name"],
                "last": spot["last"],
                "changePct": spot["pct"],
                "bid": spot["bid"],
                "ask": spot["ask"],
                "high": spot["high"],
                "low": spot["low"],
                "volume": spot["volume"],
                "volumeUnit": spot.get("volume_unit", "份"),
                "sourceTime": spot["time"],
                "secondarySource": spot["secondary_source"],
                "secondaryTime": spot["secondary_time"],
                "quality": selection["quality"],
                "priceQuality": selection["price_quality"],
                "modelQuality": selection["model_quality"],
                "conclusion": selection["conclusion"],
                "bullPermission": indicator["bull_permission"],
                "bearPermission": indicator["bear_permission"],
                "indicators": {
                    "ma5": indicator["ma"]["5"],
                    "ma20": indicator["ma"]["20"],
                    "macd": indicator["macd_hist"],
                    "adx": indicator["adx"],
                    "atr": indicator["atr14"],
                    "z20": indicator["z20"],
                },
                "bull": build_strategy(raw, code, "bull"),
                "bear": build_strategy(raw, code, "bear"),
            }
        )

    return {
        "dataset": {
            "asOfDate": raw["asof_date"],
            "generatedAt": raw["run_at"],
            "label": raw.get("snapshot_label", "历史最近快照 · 收盘冻结"),
            "qualityLabel": raw.get("quality_label", "数据质量待复核"),
            "verifiedTradingDay": raw["trading_day_verified"],
            "riskFree": raw["risk_free"] * 100,
            "riskFreeVerified": raw.get("risk_free_meta", {}).get("verified", False),
            "riskFreeSource": raw.get("risk_free_meta", {}).get("source", "未提供"),
            "sourceNote": "价格层：交易所官方主数据、官方盘口与独立行情源交叉；模型层单独评级，免费源无交易所级 SLA。",
            "cloudMode": True,
        },
        "captureSlots": raw["capture_slots"],
        "instruments": instruments,
        "totalFirstChoice": raw["selections"]["total_first_choice"],
    }


def main() -> None:
    raw = json.loads(SOURCE.read_text(encoding="utf-8"))
    dashboard = build_dashboard(raw)
    if len(dashboard["instruments"]) != 5:
        raise RuntimeError("Dashboard audit failed: expected five underlyings")
    TARGET.write_text(json.dumps(dashboard, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"dashboard": str(TARGET), "generatedAt": dashboard["dataset"]["generatedAt"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
