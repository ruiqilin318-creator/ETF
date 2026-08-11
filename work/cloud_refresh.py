from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from generate_option_report import is_trading_day


ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEDULE_TO_SLOT = {
    "46 1 * * 1-5": "09:45",
    "31 3 * * 1-5": "11:30",
    "31 5 * * 1-5": "13:30",
    "31 6 * * 1-5": "14:30",
    "1 7 * * 1-5": "15:00",
}
MAX_DELAY_MINUTES = 5


def main() -> None:
    now = datetime.now(SHANGHAI)
    event_name = os.getenv("GITHUB_EVENT_NAME", "manual")
    schedule = os.getenv("GITHUB_EVENT_SCHEDULE", "")
    expected_slot = SCHEDULE_TO_SLOT.get(schedule)

    if event_name == "schedule":
        if not expected_slot:
            raise RuntimeError(f"Unknown schedule expression: {schedule!r}")
        if not is_trading_day(now.date()):
            print(json.dumps({"status": "closed", "date": now.date().isoformat()}, ensure_ascii=False))
            return
        hour, minute = (int(value) for value in expected_slot.split(":"))
        planned = datetime.combine(now.date(), time(hour, minute), SHANGHAI)
        delay_minutes = (now - planned).total_seconds() / 60
        if delay_minutes < -2 or delay_minutes > MAX_DELAY_MINUTES:
            raise RuntimeError(
                f"Scheduled run outside freshness window: slot={expected_slot}, actual={now.isoformat()}, "
                f"delay={delay_minutes:.1f}m"
            )

    subprocess.run([sys.executable, str(ROOT / "work" / "generate_option_report.py")], cwd=ROOT, check=True)
    subprocess.run([sys.executable, str(ROOT / "work" / "export_dashboard_data.py")], cwd=ROOT, check=True)

    report_path = ROOT / "data" / "option_report_data.json"
    dashboard_path = ROOT / "data" / "dashboard_data.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))

    if report["asof_date"] != now.date().isoformat():
        raise RuntimeError(f"Stale source date: {report['asof_date']} != {now.date().isoformat()}")
    if not report["trading_day_verified"]:
        raise RuntimeError("Trading-day/source-date audit failed; refusing to publish")
    if expected_slot and report["capture_slot"] != expected_slot:
        raise RuntimeError(f"Capture slot mismatch: {report['capture_slot']} != {expected_slot}")
    if dashboard["dataset"]["generatedAt"] != report["run_at"]:
        raise RuntimeError("Dashboard/report generation timestamp mismatch")

    print(
        json.dumps(
            {
                "status": "verified",
                "slot": report["capture_slot"],
                "runAt": report["run_at"],
                "asOfDate": report["asof_date"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
