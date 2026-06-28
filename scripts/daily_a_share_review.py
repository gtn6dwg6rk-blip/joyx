#!/usr/bin/env python3
"""Daily A-share market review and next-day watchlist push.

Runs well in GitHub Actions. Secrets are read from environment variables:
OPENAI_API_KEY, WXPUSHER_APP_TOKEN, WXPUSHER_UIDS.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


EASTMONEY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}

INDEXES = [
    ("1.000001", "上证指数"),
    ("0.399001", "深证成指"),
    ("0.399006", "创业板指"),
    ("1.000688", "科创50"),
]

STATE_DIR = Path("state")
LATEST_PICK_PATH = STATE_DIR / "latest_pick.json"


def http_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = dict(EASTMONEY_HEADERS)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 + attempt * 3)
    raise RuntimeError(f"request failed after retries: {url} ({last_error})")


def eastmoney_stock_sec_id(code: str) -> str:
    if code.startswith(("6", "9")):
        return f"1.{code}"
    return f"0.{code}"


def money_yuan_to_yi(value: Any) -> float | None:
    try:
        return round(float(value) / 100_000_000, 2)
    except (TypeError, ValueError):
        return None


def safe_float(value: Any) -> float | None:
    try:
        if value in (None, "-", ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_indexes() -> list[dict[str, Any]]:
    results = []
    fields = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170"
    for secid, name in INDEXES:
        url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields={fields}"
        data = http_json(url).get("data") or {}
        price = safe_float(data.get("f43"))
        prev = safe_float(data.get("f60"))
        change = safe_float(data.get("f169"))
        pct = safe_float(data.get("f170"))
        results.append(
            {
                "name": data.get("f58") or name,
                "code": data.get("f57") or secid,
                "close": round(price / 100, 2) if price is not None else None,
                "prev_close": round(prev / 100, 2) if prev is not None else None,
                "change": round(change / 100, 2) if change is not None else None,
                "pct": round(pct / 100, 2) if pct is not None else None,
                "amount_yi": money_yuan_to_yi(data.get("f48")),
            }
        )
    return results


def fetch_top_stocks(limit: int = 80) -> list[dict[str, Any]]:
    fields = ",".join(
        [
            "f12",
            "f14",
            "f2",
            "f3",
            "f4",
            "f5",
            "f6",
            "f7",
            "f8",
            "f9",
            "f10",
            "f15",
            "f16",
            "f17",
            "f18",
        ]
    )
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get?"
        f"pn=1&pz={limit}&po=1&np=1&fltt=2&invt=2&fid=f6&fs={urllib.parse.quote(fs)}&fields={fields}"
    )
    rows = (http_json(url).get("data") or {}).get("diff") or []
    stocks = []
    for row in rows:
        name = row.get("f14") or ""
        code = row.get("f12") or ""
        if not code or "ST" in name.upper() or "退" in name:
            continue
        stocks.append(
            {
                "code": code,
                "name": name,
                "price": safe_float(row.get("f2")),
                "pct": safe_float(row.get("f3")),
                "change": safe_float(row.get("f4")),
                "volume": row.get("f5"),
                "amount_yi": money_yuan_to_yi(row.get("f6")),
                "amplitude": safe_float(row.get("f7")),
                "turnover": safe_float(row.get("f8")),
                "pe": safe_float(row.get("f9")),
                "volume_ratio": safe_float(row.get("f10")),
                "high": safe_float(row.get("f15")),
                "low": safe_float(row.get("f16")),
                "open": safe_float(row.get("f17")),
                "prev_close": safe_float(row.get("f18")),
            }
        )
    return stocks


def fetch_kline(code: str, limit: int = 5) -> list[str]:
    secid = eastmoney_stock_sec_id(code)
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6&"
        "fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&"
        f"klt=101&fqt=1&lmt={limit}"
    )
    return ((http_json(url).get("data") or {}).get("klines")) or []


def choose_candidates(stocks: list[dict[str, Any]], count: int = 12) -> list[dict[str, Any]]:
    scored = []
    for stock in stocks:
        pct = stock.get("pct")
        amount = stock.get("amount_yi") or 0
        turnover = stock.get("turnover") or 0
        volume_ratio = stock.get("volume_ratio") or 0
        amplitude = stock.get("amplitude") or 0
        if pct is None or pct < 0.3 or pct > 10.5:
            continue
        if amount < 20:
            continue
        score = amount * 0.45 + turnover * 3.0 + volume_ratio * 10 + pct * 8 - max(0, amplitude - 12) * 5
        scored.append((score, stock))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [stock for _, stock in scored[:count]]


def collect_market_data() -> dict[str, Any]:
    indexes = fetch_indexes()
    stocks = fetch_top_stocks(100)
    candidates = choose_candidates(stocks)
    for stock in candidates[:5]:
        try:
            stock["recent_kline"] = fetch_kline(stock["code"], 5)
        except Exception as exc:  # noqa: BLE001
            stock["recent_kline_error"] = str(exc)
    total_amount = sum((item.get("amount_yi") or 0) for item in stocks)
    gainers = sum(1 for item in stocks if (item.get("pct") or 0) > 0)
    losers = sum(1 for item in stocks if (item.get("pct") or 0) < 0)
    return {
        "generated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds"),
        "indexes": indexes,
        "top_amount_stocks": stocks[:30],
        "candidate_pool": candidates,
        "top100_amount_yi_sum": round(total_amount, 2),
        "top100_gainers": gainers,
        "top100_losers": losers,
    }


def primary_pick(market_data: dict[str, Any]) -> dict[str, Any]:
    candidates = market_data.get("candidate_pool") or market_data.get("top_amount_stocks") or []
    return candidates[0] if candidates else {}


def build_buy_plan(pick: dict[str, Any]) -> dict[str, Any]:
    close = safe_float(pick.get("price")) or 0
    high = safe_float(pick.get("high")) or close
    low = safe_float(pick.get("low")) or close
    if close <= 0:
        return {}
    return {
        "pullback_min": round(max(low, close * 0.97), 2),
        "pullback_max": round(close * 0.995, 2),
        "strong_min": round(max(close * 1.005, high * 1.001), 2),
        "strong_max": round(close * 1.03, 2),
        "no_chase_above": round(close * 1.05, 2),
        "stop_below": round(max(low * 0.995, close * 0.96), 2),
        "hard_stop_below": round(low * 0.99, 2),
    }


def save_latest_pick(market_data: dict[str, Any], report: str) -> dict[str, Any]:
    pick = primary_pick(market_data)
    state = {
        "recommendation_date": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d"),
        "generated_at": market_data.get("generated_at"),
        "pick": pick,
        "buy_plan": build_buy_plan(pick),
        "report_excerpt": report[:1200],
        "disclaimer": "仅供研究观察，不构成投资建议，不承诺收益。",
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_PICK_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def build_prompt(market_data: dict[str, Any]) -> str:
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
    pick = primary_pick(market_data)
    return f"""
你是一个谨慎的 A股短线复盘助手。请基于下面 JSON 数据，生成“晚间复盘 + 次日候选”。

要求：
1. 只推荐 1 只 A股，适合作为隔天观察候选，不要给组合。
2. 必须推荐 primary_pick 里的股票；如果数据明显异常，说明异常但仍围绕 primary_pick 给观察计划。
3. 优先考虑：资金活跃、成交额高、换手适中、当天表现强、短线情绪有延续可能的标的。
4. 必须包含：今日大盘、活跃方向、候选股票、当天股价数据、推荐逻辑、次日买入思路、风险提示。
5. 买入思路要给观察条件和价格区间思路，但不要写成命令式买入。
6. 必须写：仅供研究观察，不构成投资建议，不承诺收益。
7. 如果数据不足，明确说明数据口径，不要编造。

日期：{today}
primary_pick：
{json.dumps(pick, ensure_ascii=False, indent=2)}
数据：
{json.dumps(market_data, ensure_ascii=False, indent=2)}
""".strip()


def call_openai(prompt: str) -> str | None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    model = os.environ.get("OPENAI_MODEL", "gpt-5")
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": "你输出简洁、克制、可执行的中文A股复盘。不要承诺收益。",
            },
            {"role": "user", "content": prompt},
        ],
        "max_output_tokens": 1800,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("output_text"):
        return data["output_text"].strip()
    parts = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if text:
                parts.append(text)
    return "\n".join(parts).strip() or None


def fallback_report(market_data: dict[str, Any]) -> str:
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
    candidates = market_data.get("candidate_pool") or market_data.get("top_amount_stocks") or []
    pick = candidates[0] if candidates else {}
    index_lines = []
    for item in market_data.get("indexes", []):
        index_lines.append(f"{item.get('name')} {item.get('close')}，涨跌幅 {item.get('pct')}%，成交额约 {item.get('amount_yi')} 亿")
    return textwrap.dedent(
        f"""
        A股晚间复盘与次日候选 {today}

        今日大盘：
        {chr(10).join(index_lines) if index_lines else '指数数据暂缺。'}

        市场情绪：
        大成交额前100只股票中，上涨 {market_data.get('top100_gainers')} 只，下跌 {market_data.get('top100_losers')} 只，成交额合计约 {market_data.get('top100_amount_yi_sum')} 亿。该口径只统计高成交样本，不代表全市场。

        次日候选：
        {pick.get('name', '暂无')} {pick.get('code', '')}

        当天股价数据：
        收盘价 {pick.get('price')}，涨跌幅 {pick.get('pct')}%，成交额约 {pick.get('amount_yi')} 亿，换手率 {pick.get('turnover')}%，振幅 {pick.get('amplitude')}%。

        推荐逻辑：
        1. 在高成交额样本中资金关注度靠前。
        2. 当日维持红盘或相对强势，短线情绪有延续观察价值。
        3. 换手和成交额较活跃，适合隔天观察承接。

        买入思路：
        隔天不宜无脑追高。优先观察开盘后 15-30 分钟能否维持红盘、回踩不破分时均线且成交继续活跃；若高开过多或快速跳水，暂不执行原观察思路。风控线可参考当日低点或跌破关键分时支撑。

        风险提示：
        1. 高成交强势股容易出现获利盘兑现。
        2. 若大盘缩量或活跃板块退潮，候选股延续性会下降。
        3. 数据来自公开行情接口，可能存在延迟或口径差异。

        仅供研究观察，不构成投资建议，不承诺收益。
        """
    ).strip()


def push_wxpusher(content: str) -> dict[str, Any]:
    app_token = os.environ.get("WXPUSHER_APP_TOKEN")
    uids_raw = os.environ.get("WXPUSHER_UIDS", "")
    uids = [item.strip() for item in uids_raw.split(",") if item.strip()]
    if not app_token or not uids:
        return {"ok": False, "reason": "missing WXPUSHER_APP_TOKEN or WXPUSHER_UIDS"}
    payload = {
        "appToken": app_token,
        "content": content,
        "summary": "A股晚间复盘与次日候选",
        "contentType": 1,
        "uids": uids,
    }
    return http_json("https://wxpusher.zjiecode.com/api/send/message", method="POST", payload=payload)


def main() -> int:
    try:
        market_data = collect_market_data()
        report = call_openai(build_prompt(market_data)) or fallback_report(market_data)
        latest_pick = save_latest_pick(market_data, report)
        push_result = push_wxpusher(report)
        print(report)
        print("\n--- Latest pick state ---")
        print(json.dumps(latest_pick, ensure_ascii=False, indent=2))
        print("\n--- WxPusher result ---")
        safe_result = dict(push_result)
        if "appToken" in safe_result:
            safe_result["appToken"] = "***"
        print(json.dumps(safe_result, ensure_ascii=False, indent=2))
        return 0 if push_result.get("code") == 1000 or push_result.get("ok") is not False else 1
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
