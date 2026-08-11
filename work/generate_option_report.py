from __future__ import annotations

import json
import math
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
OUTPUTS = ROOT / "outputs"
SHANGHAI = ZoneInfo("Asia/Shanghai")
RUN_AT = datetime.now(SHANGHAI)
ASOF_DATE = RUN_AT.date()
VALUATION_TIME = RUN_AT.replace(tzinfo=None)
RISK_FREE = 0.0115
RISK_FREE_META = {
    "value": RISK_FREE,
    "as_of": "2026-08-07",
    "source": "çŸ­ç«¯æ— é£é™©åˆ©ç‡æ¨¡å‹å‡è®¾ï¼ˆæœªä½œä¸ºè¡Œæƒ…äº‹å®ï¼‰",
    "verified": False,
}

# ä¸Šäº¤æ‰€å…¬å¸ƒçš„2026å¹´Aè‚¡å¸‚åœºä¼‘å¸‚æ—¥ã€‚å‘¨æœ«ç”± is_trading_day å•ç‹¬å¤„ç†ã€‚
OFFICIAL_CLOSED_DATES_2026 = {
    date(2026, 1, 1), date(2026, 1, 2),
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19),
    date(2026, 2, 20), date(2026, 2, 23),
    date(2026, 4, 6),
    date(2026, 5, 1), date(2026, 5, 4), date(2026, 5, 5),
    date(2026, 6, 19),
    date(2026, 9, 25),
    date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7),
}
UNDERLYINGS = {
    "510050": {"exchange": "SSE", "symbol": "sh510050", "name": "ä¸Šè¯50ETF"},
    "510300": {"exchange": "SSE", "symbol": "sh510300", "name": "æ²ªæ·±300ETF"},
    "510500": {"exchange": "SSE", "symbol": "sh510500", "name": "ä¸­è¯500ETF"},
    "159915": {"exchange": "SZSE", "symbol": "sz159915", "name": "åˆ›ä¸šæ¿ETF"},
    "588000": {"exchange": "SSE", "symbol": "sh588000", "name": "ç§‘åˆ›50ETF"},
}

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        "Referer": "https://finance.sina.com.cn/",
    }
)


@dataclass
class Option:
    underlying: str
    exchange: str
    security_id: str
    contract_id: str
    name: str
    cp: str
    strike: float
    expiry: str
    unit: int
    bid: float
    ask: float
    bid_qty: int
    ask_qty: int
    last: float
    volume: int
    oi: int
    quote_date: str
    adjusted: bool = False
    suspended: bool = False
    official_oi: int | None = None
    vendor_iv: float | None = None
    vendor_delta: float | None = None
    vendor_gamma: float | None = None
    vendor_theta: float | None = None
    vendor_vega: float | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    q: float | None = None
    official_bid: float | None = None
    official_ask: float | None = None
    official_time: str | None = None
    official_verified: bool = False
    eastmoney_last: float | None = None
    arbitrage_ok: bool = True
    arbitrage_notes: list[str] = field(default_factory=list)
    bid_levels: list[tuple[float, int]] = field(default_factory=list)
    ask_levels: list[tuple[float, int]] = field(default_factory=list)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else self.last

    @property
    def rel_spread(self) -> float:
        m = self.mid
        return (self.ask - self.bid) / m if m > 0 and self.ask >= self.bid else float("inf")

    @property
    def dte(self) -> int:
        return (date.fromisoformat(self.expiry) - ASOF_DATE).days


def is_trading_day(value: date) -> bool:
    if value.weekday() >= 5:
        return False
    if value.year == 2026:
        return value not in OFFICIAL_CLOSED_DATES_2026
    return False


def remaining_years(expiry: str, valuation: datetime | None = None) -> float:
    valuation = valuation or VALUATION_TIME
    expiry_time = datetime.combine(date.fromisoformat(expiry), datetime.min.time()).replace(hour=15)
    return max((expiry_time - valuation).total_seconds() / (365.0 * 86400.0), 1.0 / (365.0 * 24.0))


def parse_source_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)
    for fmt, size in (("%Y%m%d%H%M%S", 14), ("%Y%m%d", 8)):
        if len(digits) >= size:
            try:
                return datetime.strptime(digits[:size], fmt)
            except ValueError:
                pass
    return None


def capture_slot_for(valuation: datetime) -> str:
    minute = valuation.hour * 60 + valuation.minute
    slots = [(585, "09:45"), (690, "11:30"), (810, "13:30"), (870, "14:30"), (900, "15:00")]
    eligible = [label for cutoff, label in slots if minute >= cutoff]
    return eligible[-1] if eligible else "09:45"


def safe_float(value: Any) -> float | None:
    try:
        x = float(value)
        if not math.isfinite(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> int:
    x = safe_float(value)
    return int(x) if x is not None else 0


def get(url: str, *, referer: str | None = None, timeout: int = 30) -> requests.Response:
    headers = {"Referer": referer} if referer else None
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = SESSION.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"GET failed: {url}: {last_error}")


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(cp: str, s: float, k: float, t: float, r: float, q: float, sigma: float) -> float:
    if t <= 1e-10 or sigma <= 1e-10:
        return max(s - k, 0.0) if cp == "C" else max(k - s, 0.0)
    st = math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * st)
    d2 = d1 - sigma * st
    if cp == "C":
        return s * math.exp(-q * t) * norm_cdf(d1) - k * math.exp(-r * t) * norm_cdf(d2)
    return k * math.exp(-r * t) * norm_cdf(-d2) - s * math.exp(-q * t) * norm_cdf(-d1)


def bs_greeks(cp: str, s: float, k: float, t: float, r: float, q: float, sigma: float) -> dict[str, float]:
    if t <= 1e-10 or sigma <= 1e-10:
        delta = 1.0 if cp == "C" and s > k else (-1.0 if cp == "P" and s < k else 0.0)
        return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    st = math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * st)
    d2 = d1 - sigma * st
    disc_q = math.exp(-q * t)
    disc_r = math.exp(-r * t)
    gamma = disc_q * norm_pdf(d1) / (s * sigma * st)
    vega = s * disc_q * norm_pdf(d1) * st
    common = -s * disc_q * norm_pdf(d1) * sigma / (2.0 * st)
    if cp == "C":
        delta = disc_q * norm_cdf(d1)
        theta = common - r * k * disc_r * norm_cdf(d2) + q * s * disc_q * norm_cdf(d1)
    else:
        delta = -disc_q * norm_cdf(-d1)
        theta = common + r * k * disc_r * norm_cdf(-d2) - q * s * disc_q * norm_cdf(-d1)
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def implied_vol(cp: str, target: float, s: float, k: float, t: float, r: float, q: float) -> float | None:
    intrinsic_lb = max(s * math.exp(-q * t) - k * math.exp(-r * t), 0.0) if cp == "C" else max(k * math.exp(-r * t) - s * math.exp(-q * t), 0.0)
    upper = s * math.exp(-q * t) if cp == "C" else k * math.exp(-r * t)
    if target < intrinsic_lb - 5e-5 or target > upper + 5e-5 or target <= 0:
        return None
    lo, hi = 0.001, 3.0
    plo = bs_price(cp, s, k, t, r, q, lo)
    phi = bs_price(cp, s, k, t, r, q, hi)
    if not (plo - 1e-8 <= target <= phi + 1e-8):
        return None
    for _ in range(90):
        mid = (lo + hi) / 2
        pmid = bs_price(cp, s, k, t, r, q, mid)
        if pmid < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def fetch_spots() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    symbols = ",".join(info["symbol"] for info in UNDERLYINGS.values())
    r = get(f"https://qt.gtimg.cn/q={symbols}", referer="https://gu.qq.com/")
    r.encoding = "gbk"
    tencent: dict[str, dict[str, Any]] = {}
    for line in r.text.strip().splitlines():
        m = re.search(r'v_(?:sh|sz)(\d+)="(.*)";', line)
        if not m:
            continue
        code, body = m.groups()
        f = body.split("~")
        tencent[code] = {
            "name": f[1],
            "last": safe_float(f[3]),
            "prev": safe_float(f[4]),
            "open": safe_float(f[5]),
            "volume": safe_int(f[6]),
            "bid": safe_float(f[9]),
            "ask": safe_float(f[19]),
            "time": f[30],
            "change": safe_float(f[31]),
            "pct": safe_float(f[32]),
            "high": safe_float(f[33]),
            "low": safe_float(f[34]),
        }
    secondary: dict[str, dict[str, Any]] = {}
    secondary_source = "ä¸œæ–¹è´¢å¯Œ"
    try:
        secids = ",".join(("0." if code == "159915" else "1.") + code for code in UNDERLYINGS)
        url = (
            "https://push2.eastmoney.com/api/qt/ulist.np/get?secids=" + secids
            + "&fields=f12,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18,f124"
        )
        em = get(url, referer="https://quote.eastmoney.com/").json()["data"]["diff"]
        for row in em:
            scale = 1000.0
            secondary[row["f12"]] = {
                "name": row.get("f14"),
                "last": row.get("f2") / scale,
                "pct": row.get("f3") / 100,
                "change": row.get("f4") / scale,
                "volume": row.get("f5"),
                "amount": row.get("f6"),
                "high": row.get("f15") / scale,
                "low": row.get("f16") / scale,
                "open": row.get("f17") / scale,
                "prev": row.get("f18") / scale,
                "time": datetime.fromtimestamp(row.get("f124"), SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
            }
    except Exception:  # Eastmoney occasionally rejects Python clients; use independent Sina spot instead.
        secondary_source = "æ–°æµª"
        sina_symbols = ",".join(info["symbol"] for info in UNDERLYINGS.values())
        sr = get(f"https://hq.sinajs.cn/list={sina_symbols}", referer="https://finance.sina.com.cn/")
        sr.encoding = "gbk"
        for line in sr.text.strip().splitlines():
            m = re.match(r'var hq_str_(?:sh|sz)(\d+)="(.*)";', line)
            if not m:
                continue
            code, body = m.groups()
            f = body.split(",")
            secondary[code] = {
                "name": f[0],
                "last": safe_float(f[3]),
                "open": safe_float(f[1]),
                "prev": safe_float(f[2]),
                "high": safe_float(f[4]),
                "low": safe_float(f[5]),
                "volume": safe_int(f[8]),
                "amount": safe_float(f[9]),
                "pct": ((safe_float(f[3]) or 0) / (safe_float(f[2]) or 1) - 1) * 100,
                "change": (safe_float(f[3]) or 0) - (safe_float(f[2]) or 0),
                "time": f"{f[30]} {f[31]}",
            }
    spots: dict[str, dict[str, Any]] = {}
    for code in UNDERLYINGS:
        tq, eq = tencent[code], secondary[code]
        tol = max(0.001, statistics.median([tq["last"], eq["last"]]) * 0.0002)
        # è…¾è®¯ f6 æ˜¯â€œæ‰‹â€ï¼ˆæ¯æ‰‹100ä»½ï¼‰ï¼Œæ–°æµª/ä¸œè´¢ä¸ºâ€œä»½â€ã€‚ç»Ÿä¸€æˆä»½åå†æ ¸éªŒã€‚
        primary_volume_shares = tq["volume"] * 100
        secondary_volume_raw = safe_int(eq.get("volume"))
        secondary_volume_shares = secondary_volume_raw * 100 if secondary_source == "ä¸œæ–¹è´¢å¯Œ" else secondary_volume_raw
        volume_validated = (
            secondary_volume_shares > 0
            and abs(primary_volume_shares - secondary_volume_shares) <= max(100, secondary_volume_shares * 0.0001)
        )
        spots[code] = {
            **tq,
            "volume_raw": tq["volume"],
            "volume_raw_unit": "æ‰‹ï¼ˆ100ä»½ï¼‰",
            "volume": primary_volume_shares,
            "volume_unit": "ä»½",
            "secondary_volume": secondary_volume_shares,
            "secondary_volume_raw": secondary_volume_raw,
            "secondary_volume_raw_unit": "æ‰‹ï¼ˆ100ä»½ï¼‰" if secondary_source == "ä¸œæ–¹è´¢å¯Œ" else "ä»½",
            "volume_validated": volume_validated,
            "secondary_source": secondary_source,
            "secondary_last": eq["last"],
            "secondary_time": eq["time"],
            "source_diff": abs(tq["last"] - eq["last"]),
            "source_tol": tol,
            "validated": abs(tq["last"] - eq["last"]) <= tol,
            "amount": eq["amount"],
        }
    return spots, {"tencent": tencent, "secondary_source": secondary_source, "secondary": secondary}


def fetch_sse_metadata() -> dict[str, dict[str, Any]]:
    url = (
        "http://query.sse.com.cn/commonQuery.do?isPagination=false&expireDate=&securityId="
        "&sqlId=SSE_ZQPZ_YSP_GGQQZSXT_XXPL_DRHY_SEARCH_L"
    )
    data = get(url, referer="https://www.sse.com.cn/assortment/options/contract/").json()["result"]
    out: dict[str, dict[str, Any]] = {}
    for row in data:
        code_m = re.search(r"\((\d{6})\)", str(row.get("SECURITYNAMEBYID", "")))
        if not code_m or code_m.group(1) not in UNDERLYINGS:
            continue
        sid = str(row["SECURITY_ID"])
        out[sid] = {
            "underlying": code_m.group(1),
            "contract_id": row.get("CONTRACT_ID", ""),
            "name": row.get("CONTRACT_SYMBOL", ""),
            "cp": "C" if row.get("CALL_OR_PUT") == "è®¤è´­" else "P",
            "strike": float(row["EXERCISE_PRICE"]),
            "expiry": datetime.strptime(row["EXPIRE_DATE"], "%Y%m%d").date().isoformat(),
            "unit": int(float(row["CONTRACT_UNIT"])),
            "adjusted": row.get("CONTRACTFLAG") != "å¦" or row.get("CHANGEFLAG") != "å¦",
            "suspended": row.get("DELISTFLAG") != "å¦",
            "timesave": row.get("TIMESAVE"),
        }
    return out


def fetch_szse_metadata() -> dict[str, dict[str, Any]]:
    url = "https://www.sse.org.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=option_drhy&TABKEY=tab1"
    content = get(url, referer="https://www.szse.cn/option/quotation/contract/daycontract/index.html").content
    df = pd.read_excel(BytesIO(content))
    out: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        target = str(row.get("æ ‡çš„è¯åˆ¸ç®€ç§°(ä»£ç )", ""))
        if "159915" not in target:
            continue
        sid = str(int(row["åˆçº¦ç¼–ç "]))
        out[sid] = {
            "underlying": "159915",
            "contract_id": str(row["åˆçº¦ä»£ç "]),
            "name": str(row["åˆçº¦ç®€ç§°"]),
            "cp":×5îÚ$z{-®éÜj×w¶òææÖWÒ¶òç6V7W&—G•ö–G×Ç¶òæW‡—'—×Ç¶òæ&–C¢ãFgÒ÷¶òæ6³¢ãFg×Ç¶òç&VÅ÷7&VB£¢ãgÒWÇ¶òçföÇVÖS¢ÇÒ÷¶òæö“¢Ç×Ç·fVæF÷%ö—e÷FW‡GÒ÷²†òæ—b÷"’£¢ãgÒWÇ·fVæF÷%öFVÇF÷FW‡GÒ÷²†òæFVÇF÷"“¢²ã6g×Ç²†òævÖÖ÷"“¢ã6g×Ç·F†WF÷&Ö#¢²ãg×Ç·fVv÷&Ö#¢²ãg×Ç·fW&–g—×Â ¢¢rÒ6öÖ&õ²&w&VV·2%Ğ¢Æ–æW2æVæB‚""¢Æ–æW2æVæB€¢b.{¸NY„w&VV·>ûÉ¤UDnjøşXùXªƒãXX>X‰ŞZx´FVÇF[ÛY8Ò¶u²vFVÇF÷&Ö%÷W%óãuÓ¢²ãgÒXX>ûÉµF†WF¶u²wF†WF÷&Ö%÷W%öF’uÓ¢²ãgÒXX2şˆz®xKniz^ûÉ² ¢b%fVv¶u²wfVv÷&Ö%÷W%ö—e÷ö–çBuÓ¢²ãgÒXX2ô•nXùXÉcKŠ®y›îXˆnx+8%B³^iÈXÛ™šKØŞ{Úî{ªb·66Vå²wv÷'7E÷CUöÖ÷fRuÒ£¢²ãgÒ^ûÈÎjŠYè¾hÙşy¸¢¶ÖöæW’‡66Vå²wv÷'7E÷CU÷æÂuÒ—ÒXX>ûÉ² ¢b.iÈZJ~iØ>XŠ˜yš8î™š’·66Vå²vÖ…÷&VÖ—VÕ÷&—6²uÓ¢ÂãgÒXX>8" ¢¢–b66Vå²&W‡—'•ö'&V¶WfVç2%Ó ¢&RÒ.8"æ¦ö–â†b'·7÷E²vÆ7BuÒ¢ƒ·‚“¢ã6gÒ‡·‚£¢²ãgÒR’"f÷"‚–â66Vå²&W‡—'•ö'&V¶WfVç2%Ò¢Æ–æW2æVæB†b.{¹şKˆhÈ‹è>i™®X‹iÉşiz^KËzé~y¨NjŠYè¾Y¹îiÊÎKØŞ{ÚîûÉ§¶&WŞ8.‹ziÈˆ[şYÊ‹è>izX‹iÉşYî™È˜xŞ[»®ûÈÎYºjÚNK¸^KÙÎš8î™šZé®KØŞ8""¢Æ–æW2æVæB€¢$•nYÎjÚ^Xk.X{¾ûÈj~y¨NKˆŞXù8B³ûÈûÉ¢ ¢².ûÈÂ"æ¦ö–â†b'¶–çB‡“¢¶G×¶ÖöæW’‡b—ŞXX2"f÷"Âb–â6÷'FVB‚‚†fÆöB†²’ÂfÂ’f÷"²ÂfÂ–â66Vå²&—e÷7G&W72%Òæ—FV×2‚’’Â¶W“ÖÆÖ&Fƒ¢…³Ò’¢².8" ¢¢Æ–æW2æVæB‚""¢–b6öÖ&òævWB‚&ÇFW&æF—fW2"“ ¢ÇE÷FW‡BÒµĞ¢f÷"ÇB–â6öÖ&õ²&ÇFW&æF—fW2%Õ³£%Ó ¢ÒÂ’Ò'•ö–E¶ÇE²&Ö–åö–B%ÕÒÂ'•ö–E¶ÇE²&–ç7W&æ6Uö–B%ÕĞ¢ÇE÷FW‡BæVæB†b'¶ÇE²våöÖ–âu×Ü9w¶Òç7G&–¶S¦w×¶Òæ7Ò·¶ÇE²våö–ç2u×Ü9w¶’ç7G&–¶S¦w×¶’æ7ŞûÈ‡¶ÇE²w66÷&RuÓ¢ãgŞXˆnûÈ’"¢Æ–æW2æVæB‚.‰Ş˜X	˜ûÉ¢"².ûÉ²"æ¦ö–â†ÇE÷FW‡B’².8.K‹¾Šh[zî[È.K‹®K»~[zî8KùŞ™šŠhny¹nh‰eF†WFşh‰iÊÎh
~K»~jùN8""¢Æ–æW2æVæB‚"" ¢'VÆÂÂ&V"Ò6VÂævWB‚&'VÆÂ"’Â6VÂævWB‚&&V""¢–b'VÆÂæB&V# ¢Æ–æW2æVæB‚"222j~y¨Nkj‹xÎh8^išşhÙşy¸®ûÈXX>ûÈ’"¢Æ–æW2æVæB‚""¢Æ–æW2æVæB‚'Îj~y¨NXùXª‡ÎXşZI¥B³ÎXşZI¥B³7ÎXşZI¥B³WÎXşz›¥B³ÎXşz›¥B³7ÎXşz›¥B³WÂ"¢Æ–æW2æVæB‚'ÂÒÒÓ§ÂÒÒÓ§ÂÒÒÓ§ÂÒÒÓ§ÂÒÒÓ§ÂÒÒÓ§ÂÒÒÓ§Â"¢f÷"–â&ævR‚Ó’Â“ ¢"Ò'VÆÅ²'66Væ&–ò%Õ²'F&ÆR%Õ·7G"‡•Ğ¢BÒ&V%²'66Væ&–ò%Õ²'F&ÆR%Õ·7G"‡•Ğ¢Æ–æW2æVæB†b'Ç·¢¶GÒWÇ¶ÖöæW’†%²wCuÒ—×Ç¶ÖöæW’†%²wC2uÒ—×Ç¶ÖöæW’†%²wCRuÒ—×Ç¶ÖöæW’†E²wCuÒ—×Ç¶ÖöæW’†E²wC2uÒ—×Ç¶ÖöæW’†E²wCRuÒ—×Â"¢Æ–æW2æVæB‚""¢Æ–æW2æVæB€¢.{ª®[è¾ûÉ®{¸NYK¨şhÙòÓ^ŠÚnh‰.8ÓR^XxşK¹>8Ó#^jÚ.hÙşûÉ³.ZJiÊ®‹[[Ë®iKn{J~ûÈÃ>ZJjŠ®y¹XxşZJ~˜:ûÈÎiÈ™[ó^KŠ®KªNi‰>iz^˜xŞŠøN8" ¢¢Æ–æW2æVæB‚"" ¢Æ–æW2æVæB‚"22ikk9^8™™X‹nKˆîy»Nhê^i[hÚîk©"¢Æ–æW2æVæB‚""¢Æ–æW2æVæB‚"ÒZé®K»~ûÉ®YNˆ[şhÈYÎjÚ^KŠŞK»~XøŞŠz4%4Ş™©Y
¾k:.Xªxè~ûÉ¾yúŞzºşizš8î™šXŠxè~X~ŠëããR^ûÈÄUDn™©Y
¾hÈiÈiKny¸®xè~yKYÎX‹iÉş‹JŞk+Ş[›>K»~KŠŞKØŞi[KËŠê8%B³2õB³^XˆnXŠ¾hÈ“Ró~KŠ®ˆz®xKniz^ŠXxş8""¢Æ–æW2æVæB‚"Òw&VV·>Xú>[èNûÉ®ŠXh^(	Îk©(	ŞiŠşikkZ®XéşZx¾hÈ~j~ûÈÎ(	ÎjŠYè¾(	ŞiŠşhÈXúşh‰KªNKŠŞK»~XøŞhêYîy¨N{¹şKˆXú>[èN8.˜:Xˆnk*®[ˆ.k©•bôFVÇFKˆî[ˆ.YË®iØ>XŠ˜yiˆîi‹îKˆŞXË˜XŞûÈÎYºjÚNŠøNXˆnY(ÎhÙşy¸®Xú®KÛşyJjŠYè¾XÎûÉ³S““^y¨NikkZ¤w&VV·>K‹®z›¢ş[È.[‹ûÈÎYÎj~Xú®KÛşyJiÊÎYËXøŞhêXÎûÈÎ[›nKº^k{KªNh˜y¹Yîš8î™šhÈ~j~ZHŞj˜xş{«.8""¢Æ–æW2æVæB‚"ÒYË®išşûÉ®jøşKŠ®ˆ¨.x+˜	ˆ[şZèÎi[N™Ùî{«şh
~˜xŞKËûÈÎiÊ®KÛşyJ„FVÇFy»N{«şZInhêûÈÎK™şiÊ®YÊX‹iÉşX˜ŞXú®zé~Xh^YÊK»~XÎ8""¢Æ–æW2æVæB‚"Ò¾Kˆ®KªNh˜[Ù>iz^Y{ªnK‹¾i[hÚåÒ†‡GG¢ò÷VW'’ç76Ræ6öÒæ6âö6öÖÖöåVW'’æFóö—5v–æF–öãÖfÇ6RfW‡—&TFFSÒg6V7W&—G”–CÒg7Ä–CÕ54Uõ¥¥õ•5ôtu¥5…Eõ……ÅôE$…•õ4T$4…ôÂûÉµ¾Kˆ®KªNh˜iÉşiØ>ŠÎh8^iÈŞXªŠûNiˆåÒ†‡GG3¢ò÷wwrç76V–æfòæ6öÒ÷6W'f–6W2ö76÷'FÖVçBö÷F–öç2òûÉµ¾k{KªNh˜[Ù>iz^Y{ªnšUÒ†‡GG3¢ò÷wwrç7§6Ræ6âö÷F–öâ÷V÷FF–öâö6öçG&7BöF–6öçG&7Bö–æFW‚æ‡FÖÂûÉµ¾k{KªNh˜iÉşiØ>K©Nj>hê^Xú5Ò†‡GG3¢ò÷wwrç7§6Ræ6âö’öÖ&¶WB÷76¦¦‡övWEF–ÖTFFöÖ&¶WD–CÓsfÖöGVÆUG—S×&VÆ÷F–öâf6öFSÓ“ssRûÉµ¾ˆ[îŠêşxë‹J~ŠÎh8UÒ†‡GG3¢ò÷BæwF–Öræ6âòûÉµ¾ikkZ®iÉşiØ>ŠÎh8UÒ†‡GG3¢ò÷7Fö6²æf–ææ6Rç6–ææ6öÒæ6âö÷F–öâ÷V÷FW2æ‡FÖÂ8""¢Æ–æW2æVæB‚"ÒXXŞ‹KXZÎ[Èhê^Xú>izKªNi‰>h˜{ªu4ÄûÉ¾iÊÎjÊ{ª~K¸^ŠzK®ZéikiKny¹K©Nj>KˆîxºÎz¸¾ŠÎh8^k©˜	ˆ[şKˆˆ{N8.iKny¹Yîy¹Xú>K¸^Kº>ŠiÈ‹ùiKny¹[ú¾xZ~ûÈÎjÊiz^[Èy¹KˆŞXúşy»Nhê^xZ~K»~h‰KªN8""¢Æ–æW2æVæB‚"ÒjŠYè¾kX¾zé~(šZéî™˜^h‰KªNKùŞŠøûÉ¾iÊÎhª^Y®K‹®™ÙîKŠ®h
~XÉnz	Nz›nKúhşûÈÎKˆŞièNh‰h©^‹XN[»®Šêî8""¢&WGW&â%Æâ"æ¦ö–â†Æ–æW2’²%Æâ   ¦FVbÖ–â‚’ÓâæöæS ¢vÆö&Â4ôeôDDRÂdÅTD”ôåõD”ÔP¢–b†6GG"‡7—2ç7FF÷WBÂ'&V6öæf–wW&R"“ ¢7—2ç7FF÷WBç&V6öæf–wW&R†Væ6öF–æsÒ'WFbÓ‚"¢õUEUE2æÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VR¢tõ$²æÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VR¢7÷G2Â7÷E÷&rÒfWF6…÷7÷G2‚¢&–Ö'•öFFW2Ò·'6U÷6÷W&6UöFFWF–ÖR‡7÷E²'F–ÖR%Ò’æFFR‚’f÷"7÷B–â7÷G2çfÇVW2‚’–b'6U÷6÷W&6UöFFWF–ÖR‡7÷E²'F–ÖR%Ò—Ğ¢6V6öæF'•öFFW2Ò°¢'6U÷6÷W&6UöFFWF–ÖR‡7÷E²'6V6öæF'•÷F–ÖR%Ò’æFFR‚¢f÷"7÷B–â7÷G2çfÇVW2‚¢–b'6U÷6÷W&6UöFFWF–ÖR‡7÷E²'6V6öæF'•÷F–ÖR%Ò¢Ğ¢–bÆVâ‡&–Ö'•öFFW2’Ò ¢&—6R'VçF–ÖTW'&÷"†b.K©Nj~y¨NK‹¾k©KªNi‰>iz^KˆŞKˆˆ{NûÉ§·6÷'FVB‡&–Ö'•öFFW2—Ò"¢4ôeôDDRÒæW‡B†—FW"‡&–Ö'•öFFW2’¢$•4µôe$TUôÔUD²&5ööb%ÒÒ4ôeôDDRæ—6öf÷&ÖB‚¢6÷W&6UöFFWF–ÖW2Ò·'6U÷6÷W&6UöFFWF–ÖR‡7÷E²'F–ÖR%Ò’f÷"7÷B–â7÷G2çfÇVW2‚•Ğ¢ÆFW7E÷6÷W&6U÷F–ÖRÒÖ‚‡fÇVRf÷"fÇVR–â6÷W&6UöFFWF–ÖW2–bfÇVR—2æ÷BæöæR¢–b4ôeôDDRÂ%TåôBæFFR‚’÷"ÆFW7E÷6÷W&6U÷F–ÖRçF–ÖR‚’ãÒFFWF–ÖRç7G'F–ÖR‚#S£"Â"Tƒ¢TÒ"’çF–ÖR‚“ ¢dÅTD”ôåõD”ÔRÒFFWF–ÖRæ6öÖ&–æR„4ôeôDDRÂFFWF–ÖRç7G'F–ÖR‚#S£"Â"Tƒ¢TÒ"’çF–ÖR‚’¢VÇ6S ¢dÅTD”ôåõD”ÔRÒ%TåôBç&WÆ6R‡G¦–æfóÔæöæR¢6÷W&6UöFFW5öÖF6‚Ò6V6öæF'•öFFW2ÓÒ´4ôeôDDWĞ ¢76UöÖWFÒfWF6…÷76UöÖWFFF‚¢7¥öÖWFÒfWF6…÷7§6UöÖWFFF‚¢÷F–öç2Â6†–åöÖWFÒ'V–ÆEö÷F–öç2‡7÷G2Â76UöÖWFÂ7¥öÖWF¢ö'•öw&÷WÒW7F–ÖFU÷öæEöw&VV·2†÷F–öç2Â7÷G2¢6†–åö6†V6·2Ò'Våö6†–åö6†V6·2†÷F–öç2Â7÷G2¢–æF–6F÷'2Â†—7F÷'•÷&rÒfWF6…ö†—7F÷'•öæEö–æF–6F÷'2‚ ¢2zÊÎKˆ˜ŞXú®[»®z¸¾ZëŞX	˜kûÉ¾h¨®kXh^h˜iÈXúşˆ;ŞXZ^˜ˆ[şXXyJZéiky¹Xú2ôôZHŞjûÈÎXhŞiÈ{¸hé.YŞ8 ¢6æF–FFUöÆVw3¢F–7E·7G"Â÷F–öåÒÒ·Ğ¢f÷"6öFR–âTäDU$Å””äu3 ¢'•ö–BÒ¶òç6V7W&—G•ö–C¢òf÷"ò–â÷F–öç5¶6öFU×Ğ¢f÷"F—&V7F–öâ–â‚&'VÆÂ"Â&&V""“ ¢f÷"6öÖ&ò–â67&VVåöF—&V7F–öâ†F—&V7F–öâÂ÷F–öç5¶6öFUÒÂ7÷G5¶6öFUÕ²&Æ7B%ÒÂÆ–Ö—CÓ#Â&WV—&Uööff–6–ÃÔfÇ6R“ ¢6æF–FFUöÆVw5¶6öÖ&õ²&Ö–åö–B%ÕÒÒ'•ö–E¶6öÖ&õ²&Ö–åö–B%ÕĞ¢6æF–FFUöÆVw5¶6öÖ&õ²&–ç7W&æ6Uö–B%ÕÒÒ'•ö–E¶6öÖ&õ²&–ç7W&æ6Uö–B%ÕĞ¢f÷"6öÖ&ò–â67&VVåöfÆÆ&6µöF—&V7F–öâ†F—&V7F–öâÂ÷F–öç5¶6öFUÒÂ7÷G5¶6öFUÕ²&Æ7B%ÒÂÆ–Ö—CÓ#Â&WV—&Uööff–6–ÃÔfÇ6R“ ¢6æF–FFUöÆVw5¶6öÖ&õ²&Ö–åö–B%ÕÒÒ'•ö–E¶6öÖ&õ²&Ö–åö–B%ÕĞ¢6æF–FFUöÆVw5¶6öÖ&õ²&–ç7W&æ6Uö–B%ÕÒÒ'•ö–E¶6öÖ&õ²&–ç7W&æ6Uö–B%ÕĞ¢f÷"÷F–öâ–â6æF–FFUöÆVw2çfÇVW2‚“ ¢Ç•ööff–6–Å÷6æ6†÷B†÷F–öâ ¢6VÆV7F–öç3¢F–7E·7G"ÂF–7E·7G"Âç•ÕÒÒ·Ğ¢f÷"6öFR–âTäDU$Å””äu3 ¢6†–âÒ÷F–öç5¶6öFUĞ¢'•ö–BÒ¶òç6V7W&—G•ö–C¢òf÷"ò–â6†–çĞ¢'VÆÅ÷7G&–7BÒ67&VVåöF—&V7F–öâ‚&'VÆÂ"Â6†–âÂ7÷G5¶6öFUÕ²&Æ7B%Ò¢&V%÷7G&–7BÒ67&VVåöF—&V7F–öâ‚&&V""Â6†–âÂ7÷G5¶6öFUÕ²&Æ7B%Ò¢'VÆÅö6æF–FFW2Ò'VÆÅ÷7G&–7B÷"67&VVåöfÆÆ&6µöF—&V7F–öâ‚&'VÆÂ"Â6†–âÂ7÷G5¶6öFUÕ²&Æ7B%Ò¢&V%ö6æF–FFW2Ò&V%÷7G&–7B÷"67&VVåöfÆÆ&6µöF—&V7F–öâ‚&&V""Â6†–âÂ7÷G5¶6öFUÕ²&Æ7B%Ò¢6VÃ¢F–7E·7G"Âç•ÒÒ·Ğ¢f÷"¶W’Â6æF–FFW2–â‚‚&'VÆÂ"Â'VÆÅö6æF–FFW2’Â‚&&V""Â&V%ö6æF–FFW2’“ ¢–bæ÷B6æF–FFW3 ¢6VÅ¶¶W•ÒÒæöæP¢6öçF–çVP¢&W7BÒ6æF–FFW5³Ğ¢&W7E²&ÇFW&æF—fW2%ÒÒ6æF–FFW5³£5Ğ¢&W7E²'66Væ&–ò%ÒÒ66Væ&–õö'VæFÆR†&W7BÂ'•ö–BÂ7÷G5¶6öFUÕ²&Æ7B%Ò¢6VÅ¶¶W•ÒÒ&W7@¢6VÆV7F–öç5¶6öFUÒÒ6VÀ ¢†—7F÷'•öFFW5öÖF6‚ÒÆÂ†–æF–6F÷'5¶6öFUÕ²&FFR%ÒÓÒ4ôeôDDRæ—6öf÷&ÖB‚’f÷"6öFR–âTäDU$Å””äu2¢G&F–æuöF•÷fW&–f–VBÒ—5÷G&F–æuöF’„4ôeôDDR’æB6÷W&6UöFFW5öÖF6‚æB†—7F÷'•öFFW5öÖF6€¢f÷"6öFR–âTäDU$Å””äu3 ¢6VÂÒ6VÆV7F–öç5¶6öFUĞ¢'•ö–BÒ¶òç6V7W&—G•ö–C¢òf÷"ò–â÷F–öç5¶6öFU×Ğ¢6VÆV7FVEö6öÖ&÷2Ò·6VÂævWB†¶W’’f÷"¶W’–â‚&'VÆÂ"Â&&V""’–b6VÂævWB†¶W’•Ğ¢†5öfÆÆ&6²Òç’†6öÖ&òævWB‚&—5öfÆÆ&6²"ÂfÇ6R’f÷"6öÖ&ò–â6VÆV7FVEö6öÖ&÷2¢6VÅ²&†5öfÆÆ&6²%ÒÒ†5öfÆÆ&6°¢&–6U÷fÆ–BÒ&ööÂ‡6VÆV7FVEö6öÖ&÷2’æB7÷G5¶6öFUÕ²'fÆ–FFVB%ÒæB7÷G5¶6öFUÕ²'föÇVÖU÷fÆ–FFVB%ÒæBG&F–æuöF•÷fW&–f–V@¢ÖöFVÅ÷fÆ–BÒ&ööÂ‡6VÆV7FVEö6öÖ&÷2’æB$•4µôe$TUôÔUD²'fW&–f–VB%Ğ¢f÷"¶W’–â‚&'VÆÂ"Â&&V""“ ¢6öÖ&òÒ6VÂævWB†¶W’¢–bæ÷B6öÖ&ó ¢6öçF–çVP¢f÷"ö–B–â†6öÖ&õ²&Ö–åö–B%ÒÂ6öÖ&õ²&–ç7W&æ6Uö–B%Ò“ ¢òÒ'•ö–E¶ö–EĞ¢&–6U÷fÆ–BÒ&–6U÷fÆ–BæBòæöff–6–Å÷fW&–f–VBæBòçVæ—BÓÒæBæ÷BòæF§W7FVBæBæ÷Bòç7W7VæFV@¢ÖöFVÅ÷fÆ–BÒÖöFVÅ÷fÆ–BæBòæ&&—G&vUöö°¢ÖöFVÅ÷fÆ–BÒÖöFVÅ÷fÆ–BæB6öÖ&õ²&w&VVµ÷&V6öæ6–Æ–F–öâ%Õ²'7FGW2%ÒÓÒ%52 ¢6VÅ²'&–6U÷VÆ—G’%ÒÒ$ûÈZéikXX>i[hÚîûÈ¾XøÎk©iKny¹hª^K»~Kˆˆ{NûÈ’"–b&–6U÷fÆ–BVÇ6R$>ûÈK»~jÎ™zzhiÊ®ZèÎi[N˜	®‹ø~ûÈ’ ¢6VÅ²&ÖöFVÅ÷VÆ—G’%ÒÒ$ûÈjŠYè¾Kˆîk©XÎKˆˆ{NûÈ’"–bÖöFVÅ÷fÆ–BVÇ6R$.ûÈ„w&VV·>™ÈZHŞjûÈşXŠxè~K‹®X~ŠëîûÈ’ ¢6VÅ²'VÆ—G’%ÒÒ€¢$.ûÈXYÎ[©^Šx.Zùş8;¾™Ùîšin˜ûÈ’ ¢–b&–6U÷fÆ–BæB†5öfÆÆ&6°¢VÇ6R$ûÈK»~jÎKˆîjŠYè¾YØ~˜	®‹ø~ûÈ’ ¢–b&–6U÷fÆ–BæBÖöFVÅ÷fÆ–@¢VÇ6R$.ûÈK»~jÎ[{.jš¨Î8;¾jŠYè¾Šx.ZùşûÈ’ ¢–b&–6U÷fÆ–@¢VÇ6R$>ûÈKˆŞhêˆÙûÈ’ ¢¢G&VæBÒ–æF–6F÷'5¶6öFUĞ¢–b†5öfÆÆ&6³ ¢6VÅ²&6öæ6ÇW6–öâ%ÒÒ.XYÎ[©^Šx.ZùşûÈÎKˆŞhš~ŠÂ ¢VÆ–bæ÷B6VÅ²'VÆ—G’%Òç7F'G7v—F‚‚$"“ ¢6VÅ²&6öæ6ÇW6–öâ%ÒÒ.Šx.ZùşûÈÎKˆŞhš~ŠÂ ¢VÆ–bG&VæE²&'VÆÅ÷W&Ö—76–öâ%ÒÓÒ.KÉXX‚"æB6VÂævWB‚&'VÆÂ"“ ¢6VÅ²&6öæ6ÇW6–öâ%ÒÒ.XşZI®KÉXX‚ ¢VÆ–bG&VæE²&&V%÷W&Ö—76–öâ%ÒÓÒ.KÉXX‚"æB6VÂævWB‚&&V""“ ¢6VÅ²&6öæ6ÇW6–öâ%ÒÒ.Xşz›®KÉXX‚ ¢VÇ6S ¢6VÅ²&6öæ6ÇW6–öâ%ÒÒ.Šx.iÉ²  ¢&æ¶VBÒµĞ¢f÷"6öFR–âTäDU$Å””äu3 ¢6VÂÂG&VæBÒ6VÆV7F–öç5¶6öFUÒÂ–æF–6F÷'5¶6öFUĞ¢–bæ÷B6VÅ²'VÆ—G’%Òç7F'G7v—F‚‚$"“ ¢6öçF–çVP¢–bG&VæE²&'VÆÅ÷W&Ö—76–öâ%ÒÓÒ.KÉXX‚"æB6VÂævWB‚&'VÆÂ"’æBæ÷B6VÅ²&'VÆÂ%ÒævWB‚&—5öfÆÆ&6²"ÂfÇ6R“ ¢&æ¶VBæVæB‚‡6VÅ²&'VÆÂ%Õ²'66÷&R%ÒÂ6öFRÂ.XşZI¢"Â6VÅ²&'VÆÂ%Ò’¢–bG&VæE²&&V%÷W&Ö—76–öâ%ÒÓÒ.KÉXX‚"æB6VÂævWB‚&&V""’æBæ÷B6VÅ²&&V"%ÒævWB‚&—5öfÆÆ&6²"ÂfÇ6R“ ¢&æ¶VBæVæB‚‡6VÅ²&&V"%Õ²'66÷&R%ÒÂ6öFRÂ.Xşz›¢"Â6VÅ²&&V"%Ò’¢–b&æ¶VC ¢&æ¶VBç6÷'B‡&WfW'6SÕG'VRÂ¶W“ÖÆÖ&Fƒ¢…³Ò¢66÷&RÂ6öFRÂF—&V7F–öâÂ6öÖ&òÒ&æ¶VE³Ğ¢'•ö–BÒ¶òç6V7W&—G•ö–C¢òf÷"ò–â÷F–öç5¶6öFU×Ğ¢Ö–åöòÂ–ç5öòÒ'•ö–E¶6öÖ&õ²&Ö–åö–B%ÕÒÂ'•ö–E¶6öÖ&õ²&–ç7W&æ6Uö–B%ÕĞ¢6VÆV7F–öç5²'F÷FÅöf—'7Eö6†ö–6R%ÒÒ€¢b'¶6öFWÒ¶F—&V7F–öçŞûÉ§¶6öÖ&õ²våöÖ–âu×Ü9w¶Ö–åöòææÖWÒ²¶6öÖ&õ²våö–ç2u×Ü9w¶–ç5öòææÖWŞûÈz	Nz›nŠøNXˆg·66÷&S¢ãgŞûÈÎK¸^KÙÎjÊiz^˜xŞikhª^K»~X˜ŞX	˜ûÈ’ ¢¢VÇ6S ¢6VÆV7F–öç5²'F÷FÅöf—'7Eö6†ö–6R%ÒÒ.iÊÎi{nx+iz{ª~K‰NikY	ŠëXúşy¨NXúşhš~ŠÎšin˜’  ¢&W÷'BÒ'V–ÆEöÖ&¶F÷vâ‡7÷G2Â–æF–6F÷'2Â÷F–öç2Â6VÆV7F–öç2Âö'•öw&÷W¢&W÷'E÷F‚ÒõUEUE2òb$UDniÉşiØ>iÈikhª^Y¥÷´4ôeôDDRæ—6öf÷&ÖB‚—Õ÷¶6GW&U÷6Æ÷Eöf÷"…dÅTD”ôåõD”ÔR’ç&WÆ6R‚s¢rÂrr—ÒæÖB ¢&W÷'E÷F‚çw&—FU÷FW‡B‡&W÷'BÂVæ6öF–æsÒ'WFbÓ‚" ¢6W&–Æ—¦&ÆUö÷F–öç2Ò¶6öFS¢¶6F–7B†ò’f÷"ò–â6†–åÒf÷"6öFRÂ6†–â–â÷F–öç2æ—FV×2‚—Ğ¢–ÆöBÒ°¢''VåöB#¢%TåôBæ—6öf÷&ÖB‚’À¢&6öeöFFR#¢4ôeôDDRæ—6öf÷&ÖB‚’À¢'&—6µög&VR#¢$•4µôe$TRÀ¢'&—6µög&VUöÖWF#¢$•4µôe$TUôÔUDÀ¢'G&F–æuöF•÷fW&–f–VB#¢G&F–æuöF•÷fW&–f–VBÀ¢'6æ6†÷EöÆ&VÂ#¢.XènXû.iÈ‹ù[ú¾xZ~ûÈş™ÙîZéîi{b+riKny¹Xk¾{¹2"–b4ôeôDDRÂ%TåôBæFFR‚’VÇ6R.[Ù>iz^Zéî™˜^h©>Xùn[ú¾xZr"À¢'VÆ—G•öÆ&VÂ#¢.K»~jÄûÙÎjŠYè´.ûÈK¸^Šx.ZùşûÈ’"–bÆÂ‡6VÆV7F–öç5¶5Õ²'&–6U÷VÆ—G’%Òç7F'G7v—F‚‚$"’f÷"2–âTäDU$Å””äu2’VÇ6R.i[hÚî™zzhiÊ®ZèÎi[N˜	®‹ør"À¢&Ö&¶WE÷7FvR#¢.iKny¹Xk¾{¹2"–bdÅTD”ôåõD”ÔRæ†÷W"ãÒRVÇ6R.KªNi‰>i{një^Zéî™˜^h©>Xùb"À¢&6GW&U÷6Æ÷B#¢6GW&U÷6Æ÷Eöf÷"…dÅTD”ôåõD”ÔR’À¢&6†–åö6†V6·2#¢6†–åö6†V6·2À¢'7÷G2#¢7÷G2À¢'7÷E÷&r#¢7÷E÷&rÀ¢&6†–åöÖWF#¢6†–åöÖWFÀ¢'ö'•öw&÷W#¢ö'•öw&÷WÀ¢&–æF–6F÷'2#¢–æF–6F÷'2À¢&÷F–öç2#¢6W&–Æ—¦&ÆUö÷F–öç2À¢'6VÆV7F–öç2#¢6VÆV7F–öç2À¢Ğ¢&6†—fUöF—"Ò$ôõBò&FF"ò'6æ6†÷G2"ò4ôeôDDRæ—6öf÷&ÖB‚¢&6†—fUöF—"æÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VR¢&6†—fUöæÖRÒ6GW&U÷6Æ÷Eöf÷"…dÅTD”ôåõD”ÔR’ç&WÆ6R‚#¢"Â""’²"æ§6öâ ¢&6†—fU÷F‚Ò&6†—fUöF—"ò&6†—fUöæÖP¢6Æ÷EöFVf–æ—F–öç2Ò°¢‚#“£CR"Â.[Èy¹zîŠêB"’Â‚#£3"Â.XØ™{NiKny¹‚"’Â‚#3£3"Â.XØYîzîŠêB"’À¢‚#C£3"Â.[îy¹Šx.Zùò"’Â‚#S£"Â.iKny¹[Ù.j2"’À¢Ğ¢6GW&U÷6Æ÷G2ÒµĞ¢f÷"6Æ÷BÂÆ&VÂ–â6Æ÷EöFVf–æ—F–öç3 ¢6Æ÷E÷F‚Ò&6†—fUöF—"òb'·6Æ÷Bç&WÆ6R‚s¢rÂrr—Òæ§6öâ ¢—5ö7W'&VçBÒ6Æ÷BÓÒ–ÆöE²&6GW&U÷6Æ÷B%Ğ¢f–Æ&ÆRÒ6Æ÷E÷F‚æW†—7G2‚’÷"—5ö7W'&Vç@¢6GW&VEöBÒ–ÆöE²''VåöB%Ò–b—5ö7W'&VçBVÇ6RæöæP¢–b6Æ÷E÷F‚æW†—7G2‚’æBæ÷B—5ö7W'&VçC ¢G'“ ¢&6†—fVE÷–ÆöBÒ§6öâæÆöG2‡6Æ÷E÷F‚ç&VE÷FW‡B†Væ6öF–æsÒ'WFbÓ‚"’¢6GW&VEöBÒ&6†—fVE÷–ÆöBævWB‚''VåöB"¢W†6WB„õ4W'&÷"Â§6öâä¥4ôäFV6öFTW'&÷"“ ¢6GW&VEöBÒæöæP¢6GW&U÷6Æ÷G2æVæB€¢°¢'F–ÖR#¢6Æ÷BÀ¢&Æ&VÂ#¢Æ&VÂÀ¢&f–Æ&ÆR#¢f–Æ&ÆRæB6GW&VEöB—2æ÷BæöæRÀ¢&6GW&VDB#¢6GW&VEöBÀ¢Ğ¢¢–ÆöE²&6GW&U÷6Æ÷G2%ÒÒ6GW&U÷6Æ÷G0¢…tõ$²ò&÷F–öå÷&W÷'EöFFæ§6öâ"’çw&—FU÷FW‡B†§6öâæGV×2‡–ÆöBÂVç7W&Uö66–“ÔfÇ6RÂ–æFVçCÓ"’ÂVæ6öF–æsÒ'WFbÓ‚"¢FFöF—"Ò$ôõBò&FF ¢FFöF—"æÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VR¢†FFöF—"ò&÷F–öå÷&W÷'EöFFæ§6öâ"’çw&—FU÷FW‡B†§6öâæGV×2‡–ÆöBÂVç7W&Uö66–“ÔfÇ6RÂ–æFVçCÓ"’ÂVæ6öF–æsÒ'WFbÓ‚"¢&6†—fU÷F‚çw&—FU÷FW‡B†§6öâæGV×2‡–ÆöBÂVç7W&Uö66–“ÔfÇ6RÂ–æFVçCÓ"’ÂVæ6öF–æsÒ'WFbÓ‚"¢&–çB†§6öâæGV×2‡²'&W÷'B#¢7G"‡&W÷'E÷F‚’Â'7VÖÖ'’#¢¶3¢¶³¢bf÷"²Âb–â6VÆV7F–öç5¶5Òæ—FV×2‚’–b²–â²'VÆ—G’"Â&6öæ6ÇW6–öâ'×Òf÷"2–âTäDU$Å””äu7ÒÂ'F÷FÅöf—'7Eö6†ö–6R#¢6VÆV7F–öç5²'F÷FÅöf—'7Eö6†ö–6R%×ÒÂVç7W&Uö66–“ÔfÇ6RÂ–æFVçCÓ"’  ¦–bõöæÖUõòÓÒ%õöÖ–åõò# ¢Ö–â‚ 