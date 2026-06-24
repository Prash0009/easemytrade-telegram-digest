#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TRACKER_ROOT = Path(
    os.getenv("NIFTY_TRACKER_ROOT")
    or os.getenv("TRACKER_ROOT")
    or "/Users/prashantkumar/Documents/Codex/nifty-oracle-tracker"
)
TRACKER_ENV = Path(os.getenv("NIFTY_TRACKER_ENV") or TRACKER_ROOT / ".env")
LOCAL_ENV = ROOT / ".env.local"
STATE_PATH = Path(os.getenv("TELEGRAM_SIGNAL_STATE_PATH") or ROOT / "data" / "telegram_signal_state.json")
SNAPSHOT_PATH = ROOT / "data" / "market.json"
COMPLETED_TRADE_LOG_PATH = ROOT / "data" / "completed_trades_log.jsonl"
MARKET_API_URL = os.getenv("EASEMYTRADE_MARKET_API_URL", "https://www.easemytrade.in/api/live-market")
INDEX_PAYLOAD_KEYS = [
    ("NIFTY", "nifty"),
    ("BANKNIFTY", "bankNifty"),
    ("FINNIFTY", "finNifty"),
    ("SENSEX", "sensex"),
]
MIN_TRIGGER_CONFIDENCE = 60
MIN_PUBLISH_CONFIDENCE = 65
TELEGRAM_SEND_START = clock_time(9, 0)
TELEGRAM_SEND_END = clock_time(16, 0)
IST = ZoneInfo("Asia/Kolkata")

if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def load_simple_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"' ")
        os.environ.setdefault(key, value)


def confidence_from_signal(action: str, score: float | None = None, change: str = "", state: str = "") -> int | None:
    try:
        normalized_score = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        normalized_score = 0.0
    if normalized_score <= 0:
        return None
    score_int = max(35, min(99, round(normalized_score)))
    if score_int >= 90:
        calibrated = 78 + (score_int - 90) * 0.8
    elif score_int >= 80:
        calibrated = 69 + (score_int - 80) * 0.9
    elif score_int >= 70:
        calibrated = 61 + (score_int - 70) * 0.8
    elif score_int >= 60:
        calibrated = 54 + (score_int - 60) * 0.75
    elif score_int >= 50:
        calibrated = 47 + (score_int - 50) * 0.7
    else:
        calibrated = 35 + (score_int - 35) * 0.8
    return max(35, min(95, round(calibrated)))


def confidence_value_from_signal(action: str, score: float | None = None, change: str = "", state: str = "") -> int:
    confidence = confidence_from_signal(action, score, change, state)
    if confidence is not None:
        return confidence
    try:
        move = abs(float(str(change).replace("%", "")))
    except (TypeError, ValueError):
        move = 0.0
    lowered = str(action or "").lower()
    if lowered in {"buy", "sell"}:
        return max(38, min(72, round(46 + move * 10)))
    if lowered == "hold":
        return max(32, min(58, round(42 - move * 3)))
    return max(35, min(60, round(40 + move * 5)))


def is_telegram_send_window(current_time: datetime | None = None) -> bool:
    current_time = current_time or datetime.now(IST)
    current_clock = current_time.time()
    return TELEGRAM_SEND_START <= current_clock <= TELEGRAM_SEND_END


def signal_hold_for(action: str, plan: dict[str, str]) -> str:
    explicit = str(plan.get("holdRule", "")).strip()
    if explicit:
        return explicit
    lowered = str(action or "").lower()
    if lowered in {"buy", "sell"}:
        return "Intraday until stop-loss, target, or 3:15 PM IST."
    if lowered == "hold":
        return "Hold only while the current range stays intact."
    return "No confirmed trade is active; await a validated break."


def format_plan_value(label: str, value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    if text and text != "--":
        return f"{label}: {text}"
    return f"{label}: {fallback}".rstrip() if fallback else f"{label}: --"


def trade_quality_from_confidence(confidence: int) -> str:
    if confidence is None:
        return "Watchlist"
    if confidence >= 65:
        return "High"
    if confidence >= 51:
        return "Medium"
    return "Low"


def trade_quality_display_text(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "high":
        return "Must Trade Zone"
    if normalized == "medium":
        return "Medium Trading Zone"
    if normalized == "low":
        return "Low Trading Zone"
    if normalized == "watchlist":
        return "Watchlist Only"
    return str(value or "--")


MACRO_EVENT_LABELS = {"RBI", "FED", "CPI", "RESULTS", "BUDGET", "GDP"}


def _safe_float(value: Any) -> float | None:
    try:
        numeric = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return numeric if numeric and numeric > 0 else None


def trade_option_type_from_text(value: Any) -> str:
    text = str(value or "").upper()
    if " CE" in text or text.endswith("CE") or "CALL" in text:
        return "CE"
    if " PE" in text or text.endswith("PE") or "PUT" in text:
        return "PE"
    return ""


def _study_card_value(study: dict[str, Any], label: str) -> str:
    cards = study.get("cards") if isinstance(study.get("cards"), list) else []
    for card in cards:
        if not isinstance(card, dict):
            continue
        if str(card.get("label") or "").strip().lower() == label.strip().lower():
            return str(card.get("value") or "").strip()
    return ""


def _option_pricing_for_label(payload: dict[str, Any], label: str) -> dict[str, Any]:
    pricing = payload.get("optionPricing") if isinstance(payload.get("optionPricing"), dict) else {}
    key = {
        "NIFTY": "nifty",
        "BANKNIFTY": "bankNifty",
        "FINNIFTY": "finNifty",
        "SENSEX": "sensex",
    }.get(label.upper(), "")
    item = pricing.get(key) if key else {}
    return item if isinstance(item, dict) else {}


def _live_option_ltp_for_signal(payload: dict[str, Any], label: str, option_type: str) -> float | None:
    pricing = _option_pricing_for_label(payload, label)
    key = "callPrice" if str(option_type or "").upper() == "CE" else "putPrice"
    source_key = "callPriceSource" if key == "callPrice" else "putPriceSource"
    if str(pricing.get(source_key) or "").strip().lower() != "live":
        return None
    try:
        value = float(str(pricing.get(key) or "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _confidence_band_label(confidence: int | None) -> str | None:
    if confidence is None:
        return None
    if confidence <= 50:
        return "0-50%"
    if confidence <= 60:
        return "51-60%"
    if confidence <= 70:
        return "61-70%"
    if confidence <= 80:
        return "71-80%"
    if confidence <= 90:
        return "81-90%"
    return "91-99%"


def market_publication_gate(
    payload: dict[str, Any],
    label: str,
    index_data: dict[str, Any],
    signal: dict[str, Any],
    plan: dict[str, Any],
    confidence: int | None,
) -> dict[str, Any]:
    study = payload.get("marketStudy") if isinstance(payload.get("marketStudy"), dict) else {}
    blockers: list[str] = []
    checks: dict[str, Any] = {}
    existing_gate = signal.get("publicationGate") if isinstance(signal.get("publicationGate"), dict) else index_data.get("publicationGate") if isinstance(index_data.get("publicationGate"), dict) else {}
    if existing_gate and (existing_gate.get("tradeRecommendation") or existing_gate.get("metaLabelDecision")):
        gate_blockers = [str(item) for item in existing_gate.get("blockers") or [] if str(item).strip()]
        recommendation = str(existing_gate.get("tradeRecommendation") or existing_gate.get("metaLabelDecision") or "").strip()
        return {
            **existing_gate,
            "publishable": bool(existing_gate.get("publishable")) and recommendation == "Trade Signal",
            "blockers": gate_blockers,
            "checks": existing_gate.get("checks") if isinstance(existing_gate.get("checks"), dict) else checks,
            "tradeRecommendation": recommendation or existing_gate.get("tradeRecommendation") or "Watchlist Only",
            "metaLabelDecision": existing_gate.get("metaLabelDecision") or recommendation or "Watchlist Only",
        }

    if not study:
        return {
            "publishable": False,
            "blockers": ["Market study is unavailable."],
            "checks": checks,
        }

    action = str(signal.get("action") or "").lower()
    if action not in {"buy", "sell"}:
        blockers.append("No directional trade signal.")
    if signal_is_completed(signal):
        blockers.append("Signal is already completed.")
    if confidence is None or confidence < MIN_PUBLISH_CONFIDENCE:
        blockers.append(f"Confidence below {MIN_PUBLISH_CONFIDENCE}% publication floor.")

    cards = {}
    for card in study.get("cards") or []:
        if isinstance(card, dict):
            cards[str(card.get("label") or "").strip().lower()] = card

    details = study.get("details") if isinstance(study.get("details"), dict) else {}
    regime_label = str((details.get("regime") or {}).get("label") or _study_card_value(study, "Regime") or "").strip()
    participation = _study_card_value(study, "Participation")
    volatility = _study_card_value(study, "Volatility")
    readiness = _study_card_value(study, "Readiness")
    minutes = _ist_minutes_from_label(signal.get("signalReportedAt") or index_data.get("signalGeneratedAt") or payload.get("signalReportedAt") or payload.get("lastUpdated") or now_ist_label())
    window_label = _publication_window_label(minutes)
    trend = details.get("trend") if isinstance(details.get("trend"), dict) else {}
    trend_rows = [item for item in (trend.get("rows") or []) if isinstance(item, dict)]
    trend_support_count = int(trend.get("supportCount") or sum(1 for row in trend_rows if row.get("support")))
    trend_score = _safe_float(trend.get("score"))
    trend_support = bool(trend_rows) and trend_support_count >= 4 and (trend_score is None or trend_score >= 55)
    checks.update(
        regime=regime_label or "--",
        participation=participation or "--",
        volatility=volatility or "--",
        readiness=readiness or "--",
        trend=f"{trend_score:.0f}%" if trend_score is not None else "--",
    )

    session_guard = _trade_session_guard(
        _review_for_label(payload, label),
        str(signal.get("signalReportedAt") or index_data.get("signalGeneratedAt") or payload.get("signalReportedAt") or payload.get("lastUpdated") or now_ist_label()),
    )
    checks["sessionGuard"] = {
        "blocked": session_guard["blocked"],
        "lossCount": session_guard["lossCount"],
        "tradeCount": session_guard["tradeCount"],
        "reasons": session_guard["reasons"],
        "latestLossAt": session_guard["latestLossAt"] or "--",
        "cooldownUntil": session_guard["cooldownUntil"] or "--",
    }
    if session_guard["blocked"]:
        blockers.append(f"Session guard: {'; '.join(session_guard['reasons'])}.")

    if readiness and any(term in readiness.lower() for term in ("watchlist only", "no trade")):
        blockers.append("Setup is still watchlist only.")
    if not trend_support:
        blockers.append("Trend-over-time is not confirming the move strongly enough.")
    if regime_label in {"Range day", "Volatile trap day"}:
        blockers.append(f"Market regime is {regime_label.lower()}.")
    if regime_label == "Expiry day" and (confidence or 0) < 80:
        blockers.append("Expiry-day setups need stronger confirmation.")
    if participation == "Weak participation":
        blockers.append("Participation is too weak.")
    if volatility == "Elevated" and regime_label not in {"Trending day", "Expiry day"}:
        blockers.append("Volatility is elevated without a clean trend regime.")

    sector_rotation = details.get("sectorRotation") if isinstance(details.get("sectorRotation"), dict) else {}
    sector_leaders = [item for item in sector_rotation.get("leaders") or [] if isinstance(item, dict)]
    if sector_leaders:
        aligned = sum(
            1
            for item in sector_leaders[:3]
            if str(item.get("tone") or "").strip().lower() == ("up" if action == "buy" else "down")
        )
        checks["sectorAlignment"] = f"{aligned}/3"
        if aligned < 2:
            blockers.append("Sector rotation is not aligned strongly enough.")
    else:
        blockers.append("Sector rotation is unavailable.")

    option_chain = details.get("optionChain") if isinstance(details.get("optionChain"), dict) else {}
    if str(option_chain.get("status") or "").strip().lower() != "available":
        blockers.append("Option-chain confirmation is unavailable.")
    else:
        pcr = _safe_float(option_chain.get("pcr"))
        writer_shift = str(option_chain.get("writerShift") or "").strip().lower()
        checks["optionChain"] = {
            "pcr": option_chain.get("pcr", "--"),
            "iv": option_chain.get("iv", "--"),
        }
        if action == "buy" and pcr is not None and pcr < 0.85:
            blockers.append("Option-chain PCR is too weak for a buy signal.")
        if action == "sell" and pcr is not None and pcr > 1.15:
            blockers.append("Option-chain PCR is too stretched for a sell signal.")
        if not writer_shift or "unavailable" in writer_shift:
            blockers.append("Writer shift is not readable.")

    volume = details.get("volume") if isinstance(details.get("volume"), dict) else {}
    if str(volume.get("status") or "").strip().lower() != "available":
        blockers.append("VWAP and volume confirmation are unavailable.")
    else:
        relation = str(volume.get("relation") or "").strip().lower()
        volume_tone = str(volume.get("volumeTone") or "").strip().lower()
        volume_ratio = _safe_float(volume.get("volumeRatio")) or 0.0
        checks["vwap"] = relation or "--"
        checks["volume"] = volume_tone or "--"
        if action == "buy":
            if relation == "below":
                blockers.append("Price is below VWAP for a buy setup.")
            if volume_tone not in {"firm", "heavy"} and volume_ratio < 1.0:
                blockers.append("Volume confirmation is not strong enough.")
        else:
            if relation == "above":
                blockers.append("Price is above VWAP for a sell setup.")
            if volume_tone not in {"firm", "heavy"} and volume_ratio < 1.0:
                blockers.append("Volume confirmation is not strong enough.")

    opening_range = details.get("openingRange") if isinstance(details.get("openingRange"), dict) else {}
    if str(opening_range.get("status") or "").strip().lower() != "available":
        blockers.append("Opening range is unavailable.")
    else:
        relation = str(opening_range.get("relation") or "").strip().lower()
        checks["openingRange"] = relation or "--"
        if action == "buy" and relation == "below":
            blockers.append("Price is still below the opening range.")
        if action == "sell" and relation == "above":
            blockers.append("Price is still above the opening range.")
        if relation == "inside" and regime_label != "Trending day":
            blockers.append("Price is still inside the opening range.")

    pivots = details.get("pivots") if isinstance(details.get("pivots"), dict) else {}
    if str(pivots.get("status") or "").strip().lower() != "available":
        blockers.append("Pivot structure is unavailable.")
    else:
        spot = _safe_float(index_data.get("level") or payload.get("nifty", {}).get("level")) or 0.0
        pivot = _safe_float(pivots.get("pivot"))
        tc = _safe_float(pivots.get("tc"))
        bc = _safe_float(pivots.get("bc"))
        checks["pivot"] = pivot
        if spot:
            if action == "buy":
                ceilings = [value for value in (pivot, tc) if value is not None]
                if ceilings and spot < max(ceilings):
                    blockers.append("Spot is below pivot/CPR resistance.")
            else:
                floors = [value for value in (pivot, bc) if value is not None]
                if floors and spot > min(floors):
                    blockers.append("Spot is above pivot/CPR support.")

    gap = details.get("gap") if isinstance(details.get("gap"), dict) else {}
    if str(gap.get("status") or "").strip().lower() != "available":
        blockers.append("Gap context is unavailable.")
    else:
        tone = str(gap.get("tone") or "").strip().lower()
        gap_percent = _safe_float(gap.get("percent")) or 0.0
        checks["gap"] = tone or "--"
        if action == "buy" and tone == "gap down" and gap_percent < -0.12:
            blockers.append("Gap-down context is against a buy signal.")
        if action == "sell" and tone == "gap up" and gap_percent > 0.12:
            blockers.append("Gap-up context is against a sell signal.")

    event_risk = details.get("eventRisk") if isinstance(details.get("eventRisk"), dict) else {}
    event_items = [str(item).strip() for item in event_risk.get("items") or [] if str(item).strip()]
    if str(event_risk.get("status") or "").strip().lower() == "crowded":
        blockers.append("Scheduled event risk is crowded.")
    if any(item.upper() in MACRO_EVENT_LABELS for item in event_items):
        blockers.append("Major macro event risk is active.")
    expiry_active = any(item.upper() == "EXPIRY" for item in event_items)
    option_side = trade_option_type_from_text(signal.get("optionType") or signal.get("option_type") or signal.get("contract") or signal.get("signal") or "")
    bucket_key = "|".join([
        label.upper(),
        option_side or "NA",
        regime_label.strip().upper() or "UNKNOWN",
        window_label.strip().upper() or "UNKNOWN",
        "EXPIRY" if expiry_active else "NON-EXPIRY",
    ])
    bucket_label = f"{label.upper()} {option_side or 'NA'} {regime_label.strip() or 'UNKNOWN'} {window_label.strip() or 'unknown'} {'expiry' if expiry_active else 'non-expiry'}".strip()

    calibration = details.get("calibration") if isinstance(details.get("calibration"), dict) else {}
    band_label = _confidence_band_label(confidence)
    calibration_contexts = calibration.get("contexts") if isinstance(calibration.get("contexts"), dict) else {}
    direction = "buy" if action == "buy" else "sell" if action == "sell" else ""
    calibration_context = calibration_contexts.get(f"{direction}|{window_label}") or calibration_contexts.get(f"{direction}|overall") or calibration_contexts.get(f"overall|{window_label}")
    calibration_context_sample = int(calibration_context.get("sample") or 0) if isinstance(calibration_context, dict) else 0
    calibration_context_win_rate = _safe_float(str(calibration_context.get("winRate") or "").replace("%", "")) if isinstance(calibration_context, dict) else None
    if band_label:
        for band in calibration.get("bands") or []:
            if not isinstance(band, dict):
                continue
            if str(band.get("band") or "").strip() != band_label:
                continue
            sample = int(band.get("sample") or 0)
            win_rate = _safe_float(str(band.get("winRate") or "").replace("%", ""))
            checks["calibration"] = f"{band_label} sample {sample}"
            if sample >= 3 and win_rate is not None and win_rate < 50:
                blockers.append(f"Historical calibration for {band_label} is weak.")
            break
    if calibration_context and calibration_context_sample >= 3 and calibration_context_win_rate is not None and calibration_context_win_rate < 50:
        blockers.append("Walk-forward calibration for this direction and time window is weak.")

    publishable = not blockers
    return {
        "publishable": publishable,
        "blockers": blockers,
        "checks": checks,
        "confidenceBand": band_label or "--",
        "bucketKey": bucket_key,
        "bucketLabel": bucket_label,
        "regimeLabel": regime_label,
        "windowLabel": window_label,
        "expiryActive": expiry_active,
    }


def signal_confidence_value(signal: dict[str, Any], index_data: dict[str, Any]) -> int:
    return confidence_value_from_signal(
        signal.get("action", ""),
        signal.get("confidenceScore") or signal.get("score"),
        index_data.get("change", ""),
        signal.get("state") or index_data.get("bias", ""),
    )


def is_trigger_qualified(confidence: int) -> bool:
    return confidence >= MIN_PUBLISH_CONFIDENCE and trade_quality_from_confidence(confidence) != "Low"


def setup_action(signal: dict[str, Any]) -> str:
    return str(signal.get("candidateAction") or signal.get("action") or "").strip()


def confidence_text(signal: dict[str, Any], index_data: dict[str, Any]) -> str:
    explicit = str(signal.get("confidence") or "").strip()
    if explicit and explicit != "--":
        return explicit if explicit.endswith("%") else f"{explicit}%"
    confidence = signal_confidence_value(signal, index_data)
    return f"{confidence}%"


def signal_is_completed(signal: dict[str, Any]) -> bool:
    status_text = " ".join(
        str(signal.get(key, "")).lower()
        for key in ("state", "statusLabel", "status")
    )
    return "completed" in status_text


def _normalize_price(value: Any) -> float:
    try:
        numeric = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0
    return numeric if numeric > 0 else 0.0


def _format_index_level(value: float) -> str:
    return f"{round(value / 10) * 10:,.0f}"


def _reward_target_level(action: str, entry_level: str, stop_level: str, fallback_level: str = "") -> str:
    entry = _normalize_price(entry_level)
    stop = _normalize_price(stop_level)
    fallback = _normalize_price(fallback_level)
    if not entry or not stop:
        return fallback_level or "--"
    risk = max(abs(stop - entry), 40.0)
    reward_floor = 650.0 if entry > 70000 else 450.0 if entry > 45000 else 100.0 if entry > 22000 else 80.0
    reward_points = max(min(risk * 2.0, reward_floor * 1.25), reward_floor)
    target = entry - reward_points if action == "Sell" else entry + reward_points
    if fallback:
        if action == "Sell" and fallback < entry:
            target = min(target, fallback)
        if action == "Buy" and fallback > entry:
            target = max(target, fallback)
    return _format_index_level(target)


def signal_bias_text(action: str) -> str:
    lowered = str(action or "").lower()
    if lowered == "buy":
        return "Bullish"
    if lowered == "sell":
        return "Bearish"
    return "Neutral"


def signal_setup_text(action: str) -> str:
    lowered = str(action or "").lower()
    if lowered == "sell":
        return "Pullback Sell (Sell on Rise)"
    if lowered == "buy":
        return "Breakout Buy (Buy on Dips)"
    return "Await Confirmed Break"


def signal_time_window_text(action: str) -> str:
    return "Immediate (next 15–30 mins)" if str(action or "").lower() in {"buy", "sell"} else "Await the next confirmed break"


def signal_entry_text(action: str, support: list[str], resistance: list[str]) -> str:
    if str(action or "").lower() == "sell":
        return f"{resistance[0]}–{resistance[1] if len(resistance) > 1 else resistance[0]} zone AFTER bearish rejection candle on 5-min"
    if str(action or "").lower() == "buy":
        return f"{support[0]}–{support[1] if len(support) > 1 else support[0]} zone AFTER bullish rejection candle on 5-min"
    return f"Await a confirmed 5-min close beyond {support[0]} / {resistance[0]}"


def signal_stop_text(plan: dict[str, Any], fallback: str = "--") -> str:
    return str(
        plan.get("stopLoss")
        or plan.get("stopLevel")
        or plan.get("stop_loss")
        or fallback
    ).strip()


def signal_target_text(plan: dict[str, Any], fallback: str = "--") -> str:
    return str(
        plan.get("targetPrice")
        or plan.get("targetLevel")
        or plan.get("target")
        or fallback
    ).strip()


def signal_reason_lines(action: str, support: list[str], resistance: list[str]) -> list[str]:
    if str(action or "").lower() == "sell":
        return [
            "Strong bearish trend with lower highs and lower lows.",
            f"Price rejecting near {resistance[0]} resistance / EMA zone.",
        ]
    if str(action or "").lower() == "buy":
        return [
            "Strong uptrend with higher highs and higher lows.",
            f"Price holding above {support[0]} support / EMA zone.",
        ]
    return [
        "Price is still inside the active range.",
        f"Awaiting a confirmed break above {resistance[0]} or below {support[0]}.",
    ]


def signal_invalidation_text(action: str, support: list[str], resistance: list[str], stop_text: str) -> str:
    if str(action or "").lower() == "sell":
        return f"If price sustains above {resistance[0]} with strong candles."
    if str(action or "").lower() == "buy":
        return f"If price sustains below {support[0]} with strong candles."
    return f"If price breaks and sustains beyond {resistance[0]} or below {support[0]}."


def full_view_url(label: str, payload: dict[str, Any]) -> str:
    base = payload.get("websiteUrl", "https://easemytrade.in").rstrip("/")
    suffix = {
        "NIFTY": "/nifty/today/",
        "BANKNIFTY": "/indices/banknifty/",
        "FINNIFTY": "/indices/finnifty/",
        "SENSEX": "/indices/sensex/",
    }.get(label.upper(), "/")
    return f"{base}{suffix}"


def index_signal(index_data: dict[str, Any]) -> dict[str, Any]:
    backend_signal = index_data.get("signal")
    if isinstance(backend_signal, dict) and backend_signal.get("action"):
        return backend_signal

    try:
        numeric = float(str(index_data.get("change", "0")).replace("%", ""))
    except ValueError:
        numeric = 0.0
    if numeric >= 0.7:
        return {
            "action": "Buy",
            "note": "Relative strength is improving; look for confirmation above the first resistance zone before acting decisively.",
            "score": None,
            "confidenceBasis": "Proxy-only index setup; direct strategy score is unavailable.",
        }
    if numeric <= -0.7:
        return {
            "action": "Sell",
            "note": "Weakness is visible and the index can be treated as a put-side setup while price remains below the first resistance band.",
            "score": None,
            "confidenceBasis": "Proxy-only index setup; direct strategy score is unavailable.",
        }
    if numeric <= -0.3:
        return {
            "action": "Sell",
            "note": "Downside pressure is building, so put-side participation is more relevant after confirmation.",
            "score": None,
            "confidenceBasis": "Proxy-only index setup; direct strategy score is unavailable.",
        }
    return {
        "action": "Hold",
        "note": "The index is inside a live range and is better treated as a monitored setup until confirmation improves.",
        "score": None,
        "confidenceBasis": "No confirmed strategy score for this proxy index setup.",
    }


def index_signal_plan(label: str, index_data: dict[str, Any], signal: dict[str, Any]) -> dict[str, str]:
    if signal.get("contract") or signal.get("entryRule"):
        return {
            "contract": str(signal.get("contract") or f"{label} WAIT").strip(),
            "entry": str(signal.get("entryRule") or signal.get("entry") or signal.get("contract") or "--").strip(),
            "stop": str(signal.get("stopLevel") or signal.get("stopRule") or signal.get("stopLoss") or "--").strip(),
            "target": str(signal.get("targetLevel") or signal.get("targetRule") or signal.get("targetPrice") or "--").strip(),
            "holdRule": signal_hold_for(signal.get("action", ""), signal),
        }

    support_levels = index_data.get("support") or ["--"]
    resistance_levels = index_data.get("resistance") or ["--"]
    support = support_levels[0]
    resistance = resistance_levels[0]
    support_target = support_levels[1] if len(support_levels) > 1 else support
    resistance_target = resistance_levels[1] if len(resistance_levels) > 1 else resistance
    try:
        spot = float(str(index_data.get("level", "0")).replace(",", ""))
    except ValueError:
        spot = 0.0
    step = 100 if label in {"BANKNIFTY", "SENSEX"} else 50
    strike = int(round(spot / step) * step) if spot else 0

    if signal["action"] == "Sell":
        contract = f"{label} {strike} PE"
        entry = f"Buy {contract} only after a 5-minute candle closes below {support}. Act only after confirmation."
        stop = resistance
        target = _reward_target_level("Sell", support, stop, support_target)
    elif signal["action"] == "Buy":
        contract = f"{label} {strike} CE"
        entry = f"Buy {contract} only after a 5-minute candle closes above {resistance}. Act only after confirmation."
        stop = support
        target = _reward_target_level("Buy", resistance, stop, resistance_target)
    else:
        contract = f"{label} WAIT"
        entry = f"Await a confirmed 5-minute candle close beyond {support} or {resistance} before considering a fresh trade."
        stop = "No fresh stop-loss until a breakout appears."
        target = "No fresh target until a directional break is in place."
    return {
        "contract": contract,
        "entry": entry,
        "stop": stop,
        "target": target,
        "holdRule": signal_hold_for(signal["action"], {}),
    }


def format_block(
    label: str,
    *,
    payload: dict[str, Any],
    index_data: dict[str, Any],
    signal: dict[str, Any],
    plan: dict[str, str],
    include_macro: bool = False,
    mode: str = "signal",
) -> str:
    confidence = signal_confidence_value(signal, index_data)
    confidence_copy = confidence_text(signal, index_data)
    trade_quality_copy = trade_quality_display_text(str(signal.get("tradeQuality") or "").strip() or trade_quality_from_confidence(confidence))
    trade_quality_copy = f"{trade_quality_copy} ({confidence_copy.split(':', 1)[-1].strip()})" if confidence_copy != "Confidence: --" else trade_quality_copy
    completed = signal_is_completed(signal)
    gate = market_publication_gate(payload, label, index_data, signal, plan, confidence)
    tradeable = not completed and is_trade_signal(signal, plan) and (mode == "hold" or (is_trigger_qualified(confidence) and gate["publishable"]))
    headline_prefix = "REVIEW" if completed else "HOLD" if mode == "hold" and tradeable else "BUY" if tradeable else "WATCH"
    headline = plan["contract"] if "WAIT" in plan["contract"] else f"{headline_prefix} {plan['contract']}"
    stop_text = str(plan.get("stop") or signal.get("stopLevel") or signal.get("stopLoss") or signal.get("stop_loss") or "--").strip()
    target_text = str(plan.get("target") or signal.get("targetLevel") or signal.get("targetPrice") or signal.get("target") or "--").strip()
    entry_text = str(plan.get("entry") or plan["contract"]).strip()
    if completed:
        return ""
    if not tradeable and mode != "hold":
        return ""

    lines = [
        headline,
        f"Reported at: {signal.get('signalReportedAt') or index_data.get('signalGeneratedAt') or payload.get('signalReportedAt', '--')}",
        f"Entry: {entry_text}",
        f"Stop Loss: {stop_text}",
        f"Target: {target_text}",
        f"Trade Quality: {trade_quality_copy}",
        f"Confidence: {confidence_copy}",
    ]
    if mode == "hold" and tradeable:
        lines.append(f"Hold: {signal_hold_for(signal.get('action', ''), plan)}")
    return "\n".join(lines)


def is_trade_signal(signal: dict[str, Any], plan: dict[str, Any]) -> bool:
    action = str(signal.get("action", "")).lower()
    contract = str(plan.get("contract") or signal.get("contract") or "").upper()
    if signal_is_completed(signal):
        return False
    if action not in {"buy", "sell"}:
        return False
    if not contract or "WAIT" in contract or "UNAVAILABLE" in contract or "NO CONFIRMED" in contract:
        return False
    return True


def signal_plan_pair(label: str, index_data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    signal = index_signal(index_data)
    plan = index_signal_plan(label, index_data, signal)
    return signal, plan


def active_trade_snapshot(payload: dict[str, Any], *, require_trigger: bool = True) -> dict[str, dict[str, Any]]:
    active: dict[str, dict[str, Any]] = {}
    for label, key in INDEX_PAYLOAD_KEYS:
        index_data = payload.get(key) or {}
        signal, plan = signal_plan_pair(label, index_data)
        effective_signal = {**signal, "action": setup_action(signal)}
        if not is_trade_signal(effective_signal, plan):
            continue
        confidence = signal_confidence_value(signal, index_data)
        gate = market_publication_gate(payload, label, index_data, signal, plan, confidence)
        trade_quality = trade_quality_from_confidence(confidence)
        # Trust the JS engine's publishable flag directly when it explicitly marks the signal
        # as a Trade Signal — the JS engine already enforces its own 78% confidence gate.
        js_gate = signal.get("publicationGate") if isinstance(signal.get("publicationGate"), dict) else {}
        js_publishable = bool(js_gate.get("publishable")) and str(js_gate.get("tradeRecommendation") or js_gate.get("metaLabelDecision") or "").strip() == "Trade Signal"
        trigger_qualified = js_publishable or is_trigger_qualified(confidence)
        if require_trigger and (not trigger_qualified or not gate["publishable"]):
            continue
        if not require_trigger and not gate["publishable"]:
            continue
        strike = str(signal.get("strike") or "").strip()
        if not strike:
            contract_parts = str(plan.get("contract") or "").split()
            if len(contract_parts) >= 3:
                strike = contract_parts[-2]
        active[label] = {
            "action": effective_signal.get("action", ""),
            "optionType": signal.get("optionType") or signal.get("option_type", ""),
            "contract": plan.get("contract", ""),
            "strike": strike,
            "entry": plan.get("entry", ""),
            "stop": plan.get("stop", ""),
            "target": plan.get("target", ""),
            "confidence": confidence_text(signal, index_data),
            "reportedConfidence": signal.get("reportedConfidence") or confidence_text(signal, index_data),
            "currentConfidence": confidence_text(signal, index_data),
            "confidenceSource": signal.get("confidenceSource") or signal.get("confidenceBasis") or "",
            "tradeQuality": trade_quality,
            "triggerQualified": trigger_qualified,
            "tradeRecommendation": gate.get("tradeRecommendation") or signal.get("tradeRecommendation") or "",
            "metaLabelDecision": gate.get("metaLabelDecision") or signal.get("metaLabelDecision") or "",
            "expectancyGate": gate.get("expectancyGate") or signal.get("expectancyGate") or {},
            "metaLabelInputs": gate.get("metaLabelInputs") or signal.get("metaLabelInputs") or {},
            "reportedAt": signal.get("signalReportedAt") or index_data.get("signalGeneratedAt") or payload.get("signalReportedAt", ""),
            "holdRule": plan.get("holdRule") or signal_hold_for(signal.get("action", ""), signal),
            "note": signal.get("note", ""),
            "watch": index_data.get("watch", ""),
            "structure": (index_data.get("analysis") or {}).get("structure", ""),
            "momentum": (index_data.get("analysis") or {}).get("momentum", ""),
            "tradePlan": (index_data.get("analysis") or {}).get("tradePlan", ""),
            "publicationGate": gate,
            "bucketKey": gate.get("bucketKey") or "",
            "bucketLabel": gate.get("bucketLabel") or "",
            "regimeLabel": gate.get("regimeLabel") or "",
            "windowLabel": gate.get("windowLabel") or "",
            "expiryActive": bool(gate.get("expiryActive", False)),
            "liveEntryLtp": _live_option_ltp_for_signal(payload, label, signal.get("option_type", "")),
            "liveEntryAt": payload.get("lastUpdated") or payload.get("signalReportedAt") or now_ist_label(),
        }
    return active


def build_observed_setups(
    candidates: dict[str, dict[str, Any]],
    previous: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    observed: dict[str, dict[str, Any]] = {}
    for label, setup in candidates.items():
        old_setup = previous.get(label)
        same_direction = (
            old_setup
            and old_setup.get("action") == setup.get("action")
        )
        observed[label] = {
            **setup,
            "reportedAt": old_setup.get("reportedAt") if same_direction and old_setup.get("reportedAt") else setup.get("reportedAt", ""),
        }
    return observed


def build_completed_trade_archive(
    payload: dict[str, Any],
    previous_active: dict[str, dict[str, Any]],
    current_active: dict[str, dict[str, Any]],
    previous_archive: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    payload_keys = {label: key for label, key in INDEX_PAYLOAD_KEYS}
    archive = [item for item in (previous_archive or []) if isinstance(item, dict)]
    current_keys = set(current_active.keys())
    for label, previous in previous_active.items():
        if label in current_keys:
            continue
        index_data = payload.get(payload_keys.get(label, ""), {}) if payload_keys.get(label, "") else {}
        signal = index_signal(index_data if isinstance(index_data, dict) else {})
        completed = signal.get("completedOutcome") if isinstance(signal, dict) else None
        if not isinstance(completed, dict) and isinstance(signal, dict):
            completed = signal.get("completed") if isinstance(signal.get("completed"), dict) else None
        if not isinstance(completed, dict):
            continue
        entry_ltp = previous.get("liveEntryLtp")
        exit_ltp = _live_option_ltp_for_signal(payload, label, previous.get("optionType") or signal.get("option_type") or signal.get("optionType") or "")
        if entry_ltp is None or exit_ltp is None or entry_ltp <= 0 or exit_ltp <= 0:
            continue
        completed_at = str(completed.get("completedAt") or payload.get("lastUpdated") or now_ist_label()).strip()
        reported_at = str(previous.get("reportedAt") or previous.get("signalReportedAt") or payload.get("signalReportedAt") or "").strip()
        if not reported_at:
            continue
        strike_text = str(previous.get("strike") or "").strip()
        if not strike_text:
            contract_parts = str(previous.get("contract") or signal.get("contract") or "").split()
            if len(contract_parts) >= 3 and contract_parts[-2].isdigit():
                strike_text = contract_parts[-2]
        pnl_ltp = (exit_ltp - entry_ltp) * _lot_size(label)
        duration_minutes = 0
        try:
            reported_dt = datetime.strptime(reported_at, "%Y-%m-%d %H:%M IST")
            completed_dt = datetime.strptime(completed_at, "%Y-%m-%d %H:%M IST")
            duration_minutes = max(5, int((completed_dt - reported_dt).total_seconds() // 60))
        except Exception:
            duration_minutes = 0
        record = {
            "label": label,
            "reportedAt": reported_at,
            "completedAt": completed_at,
            "signal": previous.get("contract") or signal.get("contract") or "",
            "strike": strike_text,
            "action": previous.get("action") or signal.get("action") or "",
            "optionType": previous.get("optionType") or signal.get("option_type") or signal.get("optionType") or "",
            "entryLiveLtp": round(float(entry_ltp), 2),
            "exitLiveLtp": round(float(exit_ltp), 2),
            "pnlLtp": round(float(pnl_ltp), 2),
            "result": str(completed.get("result") or "Completed"),
            "completionNote": str(completed.get("note") or ""),
            "duration": _format_duration(duration_minutes) if duration_minutes else "--",
            "confidence": previous.get("confidence") or "",
            "reportedConfidence": previous.get("reportedConfidence") or previous.get("confidence") or "",
            "currentConfidence": previous.get("currentConfidence") or previous.get("confidence") or "",
            "tradeQuality": previous.get("tradeQuality") or "",
            "confidenceSource": previous.get("confidenceSource") or previous.get("confidenceBasis") or "Captured live-chain signal archive",
            "confidenceAudit": "Captured live-chain entry and exit prices were archived when the signal moved to review only.",
            "holdRule": previous.get("holdRule") or "",
            "watch": previous.get("watch") or "",
            "structure": previous.get("structure") or "",
            "momentum": previous.get("momentum") or "",
            "tradePlan": previous.get("tradePlan") or "",
            "entryRule": previous.get("entry") or previous.get("entryRule") or "",
            "stopRule": previous.get("stop") or previous.get("stopRule") or "",
            "targetRule": previous.get("target") or previous.get("targetRule") or "",
            "bucketKey": previous.get("bucketKey") or previous.get("bucket_key") or signal.get("bucketKey") or "",
            "bucketLabel": previous.get("bucketLabel") or previous.get("bucket_label") or signal.get("bucketLabel") or "",
            "regimeLabel": previous.get("regimeLabel") or previous.get("regime_label") or signal.get("regimeLabel") or "",
            "windowLabel": previous.get("windowLabel") or previous.get("window_label") or signal.get("windowLabel") or "",
            "expiryActive": bool(previous.get("expiryActive") if previous.get("expiryActive") is not None else previous.get("expiry_active") if previous.get("expiry_active") is not None else signal.get("expiryActive") if signal.get("expiryActive") is not None else signal.get("expiry_active", False)),
            "mfePoints": completed.get("mfePoints") or completed.get("maxFavorableMove") or previous.get("mfePoints") or previous.get("maxFavorableMove") or signal.get("mfePoints") or signal.get("maxFavorableMove"),
            "maePoints": completed.get("maePoints") or completed.get("maxAdverseMove") or previous.get("maePoints") or previous.get("maxAdverseMove") or signal.get("maePoints") or signal.get("maxAdverseMove"),
            "maxFavorableMove": completed.get("maxFavorableMove") or completed.get("mfePoints") or previous.get("maxFavorableMove") or previous.get("mfePoints") or signal.get("maxFavorableMove") or signal.get("mfePoints"),
            "maxAdverseMove": completed.get("maxAdverseMove") or completed.get("maePoints") or previous.get("maxAdverseMove") or previous.get("maePoints") or signal.get("maxAdverseMove") or signal.get("maePoints"),
            "targetProximityRatio": completed.get("targetProximityRatio") or previous.get("targetProximityRatio") or signal.get("targetProximityRatio"),
            "adverseRatio": completed.get("adverseRatio") or previous.get("adverseRatio") or signal.get("adverseRatio"),
            "targetAlmostTouched": bool(completed.get("targetAlmostTouched") or previous.get("targetAlmostTouched") or signal.get("targetAlmostTouched")),
            "entryLooksLate": bool(completed.get("entryLooksLate") or previous.get("entryLooksLate") or signal.get("entryLooksLate")),
            "entryDiagnostic": completed.get("entryDiagnostic") or previous.get("entryDiagnostic") or signal.get("entryDiagnostic") or "",
            "reversalCandidate": bool(completed.get("reversalCandidate") or previous.get("reversalCandidate") or signal.get("reversalCandidate")),
            "diagnosticNote": completed.get("diagnosticNote") or previous.get("diagnosticNote") or signal.get("diagnosticNote") or "",
            "liveEntryAt": str(previous.get("liveEntryAt") or previous.get("reportedAt") or payload.get("signalReportedAt") or "").strip(),
            "signalReportedAt": str(payload.get("signalReportedAt") or previous.get("reportedAt") or "").strip(),
            "publicationGate": previous.get("publicationGate") if isinstance(previous.get("publicationGate"), dict) else {},
            "source": "live option-chain archive",
        }
        duplicate_key = (record["label"], record["reportedAt"], record["completedAt"], record["signal"])
        if any((item.get("label"), item.get("reportedAt"), item.get("completedAt"), item.get("signal")) == duplicate_key for item in archive):
            continue
        archive.append(record)
    return archive


def classify_signal_state(payload: dict[str, Any], state: dict[str, Any]) -> tuple[set[str], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    previous = state.get("active_trades") if isinstance(state.get("active_trades"), dict) else {}
    current = active_trade_snapshot(payload)
    candidates = active_trade_snapshot(payload, require_trigger=False)
    hold_labels: set[str] = set()
    next_active: dict[str, dict[str, Any]] = {}

    for label, trade in candidates.items():
        old_trade = previous.get(label)
        if (
            old_trade
            and old_trade.get("triggerQualified") is True
            and old_trade.get("action") == trade.get("action")
            and old_trade.get("contract") == trade.get("contract")
        ):
            hold_labels.add(label)
            next_active[label] = {
                **trade,
                **old_trade,
                "confidence": trade.get("confidence"),
                "reportedConfidence": old_trade.get("reportedConfidence") or old_trade.get("confidence") or trade.get("confidence"),
                "currentConfidence": trade.get("confidence"),
                "confidenceSource": old_trade.get("confidenceSource") or trade.get("confidenceSource", ""),
                "tradeQuality": trade.get("tradeQuality"),
            }
        elif trade.get("triggerQualified") is True:
            next_active[label] = trade

    # If a tradeable setup temporarily becomes neutral, keep reminding hold until
    # an opposite setup or a changed contract appears.
    for label, trade in previous.items():
        if label not in candidates and trade.get("triggerQualified") is True:
            hold_labels.add(label)
            next_active[label] = trade

    return hold_labels, current, next_active


def market_is_open(payload: dict[str, Any]) -> bool:
    if str(payload.get("meta", {}).get("marketStatus", "")).lower() == "open":
        return True
    return "live market" in str(payload.get("session", "")).lower() or "market hours" in str(payload.get("session", "")).lower()


def digest_signature(payload: dict[str, Any]) -> str:
    def parts(label: str, index_data: dict[str, Any], signal: dict[str, Any], plan: dict[str, Any]) -> list[str]:
        confidence = signal_confidence_value(signal, index_data)
        stop = str(plan.get("stop") or signal.get("stopLevel") or signal.get("stopLoss") or signal.get("stop_loss") or "--")
        target = str(plan.get("target") or signal.get("targetLevel") or signal.get("targetPrice") or signal.get("target") or "--")
        return [
            label,
            str(signal.get("action", "")),
            str(plan.get("contract", "")),
            str(plan.get("entry", "")),
            stop,
            target,
            str(confidence),
        ]

    signature_payload = "|".join(
        [
            item
            for label, key in INDEX_PAYLOAD_KEYS
            for item in parts(label, payload[key], *signal_plan_pair(label, payload[key]))
        ]
    )
    return hashlib.sha256(signature_payload.encode("utf-8")).hexdigest()


def build_message(
    payload: dict[str, Any],
    hold_labels: set[str] | None = None,
    active_trades: dict[str, dict[str, Any]] | None = None,
) -> str:
    hold_labels = hold_labels or set()
    active_trades = active_trades or {}
    lines = [
        "EASEMYTRADE SIGNAL DIGEST",
        (
            f"Market Closed | Last market session {payload.get('meta', {}).get('lastMarketSessionAt') or payload.get('signalReportedAt') or payload['lastUpdated']}"
            if "closed" in str(payload.get("session", "")).lower()
            else f"Live feed unavailable | Last market session {payload.get('meta', {}).get('lastMarketSessionAt') or payload.get('signalReportedAt') or payload['lastUpdated']}"
            if "stale" in str(payload.get("session", "")).lower()
            or str(payload.get("meta", {}).get("feedStatus", "")).lower() == "live-unavailable"
            else f"Updated: {payload['lastUpdated']}"
        ),
        "",
    ]
    for index, (label, key) in enumerate(INDEX_PAYLOAD_KEYS):
        signal, plan = signal_plan_pair(label, payload[key])
        active_trade = active_trades.get(label) if label in hold_labels else None
        if active_trade:
            signal = {
                **signal,
                "action": active_trade.get("action", signal.get("action", "")),
                "contract": active_trade.get("contract", signal.get("contract", "")),
                "entryRule": active_trade.get("entry", signal.get("entryRule", "")),
                "stopLevel": active_trade.get("stop", signal.get("stopLevel", "")),
                "targetLevel": active_trade.get("target", signal.get("targetLevel", "")),
                "confidence": active_trade.get("confidence", signal.get("confidence", "")),
                "tradeQuality": active_trade.get("tradeQuality", signal.get("tradeQuality", "")),
                "signalReportedAt": active_trade.get("reportedAt", signal.get("signalReportedAt", "")),
                "holdRule": active_trade.get("holdRule", signal.get("holdRule", "")),
            }
            plan = {
                **plan,
                "contract": active_trade.get("contract", plan.get("contract", "")),
                "entry": active_trade.get("entry", plan.get("entry", "")),
                "stop": active_trade.get("stop", plan.get("stop", "")),
                "target": active_trade.get("target", plan.get("target", "")),
                "holdRule": active_trade.get("holdRule", plan.get("holdRule", "")),
            }
        block = format_block(
            label,
            payload=payload,
            index_data=payload[key],
            signal=signal,
            plan={**plan, "holdRule": signal_hold_for(signal.get("action", ""), plan)},
            include_macro=label == "NIFTY",
            mode="hold" if label in hold_labels else "signal",
        )
        if not block:
            continue
        if len(lines) > 3:
            lines.append("")
        lines.append(block)
    return "\n".join(lines)


def payload_has_unavailable_indian_feed(payload: dict[str, Any]) -> bool:
    if str(payload.get("meta", {}).get("feedStatus", "")).lower() == "live-unavailable":
        return True
    for _, key in INDEX_PAYLOAD_KEYS:
        index_data = payload.get(key) or {}
        if str(index_data.get("feedStatus", "")).lower() == "live-unavailable" or str(index_data.get("level", "")) == "--":
            return True
    return False


def fetch_live_payload_once(url: str = MARKET_API_URL, *, force_live: bool = False) -> dict[str, Any]:
    separator = "&" if "?" in url else "?"
    # No `t=` cache-buster: a unique URL each call defeats the CDN edge cache and
    # forces a slow origin crawl (which timed out). The non-force read should hit
    # the ~20s edge cache (fast); only forceLive deliberately bypasses it.
    target = f"{url}{separator}forceLive=1" if force_live else url
    request = Request(
        target,
        headers={
            "Accept": "application/json",
            "User-Agent": "EaseMyTrade-Telegram-Signal-Workflow/1.0",
        },
    )
    with urlopen(request, timeout=75) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_live_payload(url: str = MARKET_API_URL) -> dict[str, Any]:
    errors: list[str] = []
    # Cached (edge) read first — it carries live data refreshed every ~20s and is
    # fast/reliable. forceLive is only a fallback when the cached payload reports
    # an unavailable Indian feed (e.g. a cold edge), since it bypasses the cache.
    for force_live in (False, True):
        try:
            payload = fetch_live_payload_once(url, force_live=force_live)
            if payload_has_unavailable_indian_feed(payload):
                errors.append(f"{'forceLive' if force_live else 'live'}: Indian live feed unavailable")
                continue
            return payload
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            errors.append(f"{'forceLive' if force_live else 'live'}: {exc}")
    raise RuntimeError("Unable to load live market payload. " + " | ".join(errors))


def build_local_payload() -> dict[str, Any]:
    if str(TRACKER_ROOT) not in sys.path:
        sys.path.insert(0, str(TRACKER_ROOT))
    from generate_market_overview import build_market_payload  # noqa: PLC0415

    return build_market_payload()


def load_snapshot_payload() -> dict[str, Any]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def load_payload(source: str = "auto") -> tuple[dict[str, Any], str]:
    source = source.lower()
    attempts = {
        "live": [("live", fetch_live_payload)],
        "local": [("local", build_local_payload)],
        "snapshot": [("snapshot", load_snapshot_payload)],
        "auto": [
            ("live", fetch_live_payload),
            ("local", build_local_payload),
            ("snapshot", load_snapshot_payload),
        ],
    }.get(source)
    if not attempts:
        raise ValueError(f"Unsupported payload source: {source}")

    errors: list[str] = []
    for label, loader in attempts:
        try:
            return loader(), label
        except (FileNotFoundError, ImportError, ModuleNotFoundError, HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            errors.append(f"{label}: {exc}")

    raise RuntimeError("Unable to load market payload. " + " | ".join(errors))


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _completed_trade_log_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("label") or "").strip().upper(),
        str(row.get("reportedAt") or "").strip(),
        str(row.get("completedAt") or "").strip(),
        str(row.get("signal") or "").strip(),
    )


def _parse_ist_label(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M IST")
    except ValueError:
        return None


def _completed_trade_log_sort_key(row: dict[str, Any]) -> datetime:
    completed_at = _parse_ist_label(str(row.get("completedAt") or ""))
    if completed_at:
        return completed_at
    reported_at = _parse_ist_label(str(row.get("reportedAt") or ""))
    return reported_at or datetime.min


def load_completed_trade_log() -> list[dict[str, Any]]:
    if not COMPLETED_TRADE_LOG_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for raw_line in COMPLETED_TRADE_LOG_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    except OSError:
        return []
    return rows


def append_completed_trade_log(
    rows: list[dict[str, Any]] | None,
    *,
    payload_source: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    completed_rows = [item for item in (rows or []) if isinstance(item, dict)]
    if not completed_rows:
        return

    existing_keys = {_completed_trade_log_key(item) for item in load_completed_trade_log()}
    appended_lines: list[str] = []
    logged_at = now_ist_label()
    market_updated_at = ""
    signal_reported_at = ""
    if isinstance(payload, dict):
        market_updated_at = str(payload.get("lastUpdated") or "").strip()
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        signal_reported_at = str(payload.get("signalReportedAt") or meta.get("signalReportedAt") or "").strip()

    for row in completed_rows:
        normalized = dict(row)
        normalized.setdefault("source", "live option-chain archive")
        normalized["loggedAt"] = normalized.get("loggedAt") or logged_at
        if payload_source:
            normalized["payloadSource"] = payload_source
        if market_updated_at:
            normalized["marketUpdatedAt"] = market_updated_at
        if signal_reported_at:
            normalized["signalReportedAt"] = signal_reported_at
        key = _completed_trade_log_key(normalized)
        if key in existing_keys:
            continue
        existing_keys.add(key)
        appended_lines.append(json.dumps(normalized, ensure_ascii=False, sort_keys=True))

    if not appended_lines:
        return

    COMPLETED_TRADE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with COMPLETED_TRADE_LOG_PATH.open("a", encoding="utf-8") as handle:
        for line in appended_lines:
            handle.write(line + "\n")


def same_ist_day(left: str = "", right: str = "") -> bool:
    left_match = re.search(r"(\d{4}-\d{2}-\d{2})", str(left or ""))
    right_match = re.search(r"(\d{4}-\d{2}-\d{2})", str(right or ""))
    return bool(left_match and right_match and left_match.group(1) == right_match.group(1))


def _is_resolved_trade_result(result: Any) -> bool:
    return str(result or "").strip().lower() in {"profit", "loss", "target hit", "stop loss hit", "trailing stop profit", "breakeven", "ambiguous"}


def _is_loss_trade_result(result: Any) -> bool:
    return str(result or "").strip().lower() in {"loss", "stop loss hit"}


def _review_for_label(payload: dict[str, Any], label: str) -> dict[str, Any] | None:
    normalized = label.upper()
    if normalized == "NIFTY":
        review = payload.get("profitReview")
    else:
        mapping = {
            "BANKNIFTY": "bankNifty",
            "FINNIFTY": "finNifty",
            "SENSEX": "sensex",
        }
        review = (payload.get("profitReviewByIndex") or {}).get(mapping.get(normalized, ""))
    return review if isinstance(review, dict) else None


def _trade_session_guard(review: dict[str, Any] | None, reference_at: str, *, loss_limit: int = 2, trade_limit: int = 3, cooldown_minutes: int = 45) -> dict[str, Any]:
    trades = list(review.get("trades") or []) if isinstance(review, dict) else []
    if not trades or not reference_at:
        return {
            "blocked": False,
            "lossCount": 0,
            "tradeCount": 0,
            "reasons": [],
            "latestLossAt": "",
            "cooldownUntil": "",
        }

    same_day_trades = [trade for trade in trades if same_ist_day(trade.get("reportedAt") or trade.get("signalReportedAt") or "", reference_at)]
    resolved_trades = [trade for trade in same_day_trades if _is_resolved_trade_result(trade.get("result"))]
    loss_trades = [trade for trade in resolved_trades if _is_loss_trade_result(trade.get("result"))]

    latest_loss_at: datetime | None = None
    for trade in loss_trades:
        candidate_text = str(trade.get("completedAt") or trade.get("reportedAt") or trade.get("signalReportedAt") or "").strip()
        try:
            candidate = datetime.strptime(candidate_text, "%Y-%m-%d %H:%M IST")
        except ValueError:
            candidate = None
        if candidate and (latest_loss_at is None or candidate > latest_loss_at):
            latest_loss_at = candidate

    reasons: list[str] = []
    if len(loss_trades) >= loss_limit:
        reasons.append(f"{len(loss_trades)} losses already logged today")
    if len(resolved_trades) >= trade_limit:
        reasons.append(f"{len(resolved_trades)} completed trades already used today")
    if latest_loss_at is not None:
        cooldown_until = latest_loss_at + timedelta(minutes=cooldown_minutes)
        try:
            reference_dt = datetime.strptime(reference_at, "%Y-%m-%d %H:%M IST")
        except ValueError:
            reference_dt = None
        if reference_dt and reference_dt <= cooldown_until:
            reasons.append(f"last stop-loss was within {cooldown_minutes} minutes")
    else:
        cooldown_until = None

    return {
        "blocked": bool(reasons),
        "lossCount": len(loss_trades),
        "tradeCount": len(resolved_trades),
        "reasons": reasons,
        "latestLossAt": latest_loss_at.strftime("%Y-%m-%d %H:%M IST") if latest_loss_at else "",
        "cooldownUntil": cooldown_until.strftime("%Y-%m-%d %H:%M IST") if cooldown_until else "",
    }


def prune_state_for_reference(state: dict[str, Any], reference_at: str) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    active_trades = state.get("active_trades") if isinstance(state.get("active_trades"), dict) else {}
    observed_setups = state.get("observed_setups") if isinstance(state.get("observed_setups"), dict) else {}

    def _keep(setup: dict[str, Any]) -> bool:
        reported_at = str(setup.get("reportedAt") or setup.get("signalReportedAt") or "").strip()
        if not reported_at:
            return False
        return same_ist_day(reported_at, reference_at)

    return {
        **state,
        "active_trades": {label: setup for label, setup in active_trades.items() if _keep(setup)},
        "observed_setups": {label: setup for label, setup in observed_setups.items() if _keep(setup)},
    }


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def now_ist_label() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")


def _ist_minutes_from_label(value: str = "") -> int | None:
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})\s+IST$", str(value or "").strip())
    if not match:
        return None
    return int(match.group(4)) * 60 + int(match.group(5))


def _publication_window_label(minutes: int | None) -> str:
    if minutes is None:
        return "unknown"
    if 9 * 60 + 30 <= minutes < 11 * 60:
        return "core-morning"
    if 11 * 60 <= minutes < 12 * 60:
        return "late-morning"
    if 12 * 60 <= minutes < 13 * 60 + 30:
        return "midday"
    if 13 * 60 + 30 <= minutes <= 14 * 60 + 30:
        return "core-afternoon"
    if 14 * 60 + 30 < minutes <= 15 * 60 + 30:
        return "late-session"
    return "outside"


def current_signal_summary(trades: dict[str, dict[str, Any]], candidates: dict[str, dict[str, Any]]) -> tuple[str, str]:
    source = trades or candidates
    for label in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
        item = source.get(label) if isinstance(source, dict) else None
        if item:
            return str(item.get("action") or ""), str(item.get("contract") or "")
    return "", ""


def update_status_state(
    state: dict[str, Any],
    *,
    payload: dict[str, Any],
    payload_source: str,
    signature: str,
    status: str,
    detail: str,
    active_trades: dict[str, dict[str, Any]],
    observed_setups: dict[str, dict[str, Any]],
    completed_trades_archive: list[dict[str, Any]] | None = None,
    candidates: dict[str, dict[str, Any]] | None = None,
    sent: bool = False,
    skipped_duplicate: bool = False,
    failed: bool = False,
) -> None:
    now_label = now_ist_label()
    action, contract = current_signal_summary(active_trades, candidates or observed_setups)
    state["last_bot_run_at"] = now_label
    state["last_signature"] = signature
    state["last_updated"] = payload["lastUpdated"]
    state["signal_reported_at"] = payload.get("signalReportedAt") or payload.get("meta", {}).get("signalReportedAt")
    state["payload_source"] = payload_source
    state["active_trades"] = active_trades
    state["observed_setups"] = observed_setups
    if completed_trades_archive is not None:
        state["completed_trades_archive"] = completed_trades_archive
        append_completed_trade_log(completed_trades_archive, payload_source=payload_source, payload=payload)
    state["telegram_status"] = status
    state["last_status_detail"] = detail
    state["current_signal_action"] = action
    state["current_signal_contract"] = contract
    if sent:
        state["last_message_sent_at"] = now_label
        state["last_message_action"] = action
        state["last_message_contract"] = contract
    if skipped_duplicate:
        state["last_skipped_duplicate_at"] = now_label
    if failed:
        state["last_failed_at"] = now_label


def message_signature(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def telegram_target(channel: str = "signal") -> tuple[str | None, str | None]:
    if channel == "signal":
        token = os.getenv("TELEGRAM_SIGNAL_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_SIGNAL_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
        return token, chat_id
    return os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")


def send_telegram(message: str, *, channel: str = "signal") -> None:
    token, chat_id = telegram_target(channel)
    if not token or not chat_id:
        raise RuntimeError("Telegram credentials are missing. Configure TELEGRAM_SIGNAL_BOT_TOKEN and TELEGRAM_SIGNAL_CHAT_ID, or the TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID fallback.")

    body = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=25) as response:
        if response.status >= 400:
            raise RuntimeError(f"Telegram API returned HTTP {response.status}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send EASEMYTRADE signal digest to Telegram.")
    parser.add_argument("--always-send", action="store_true", help="Send even if the digest is unchanged.")
    parser.add_argument("--print-only", action="store_true", help="Print the digest and do not send.")
    parser.add_argument("--record-current", action="store_true", help="Record the current digest signature without sending.")
    parser.add_argument(
        "--source",
        choices=["auto", "live", "local", "snapshot"],
        default=os.getenv("TELEGRAM_PAYLOAD_SOURCE", "auto"),
        help="Market payload source. GitHub Actions should use live to mirror the website.",
    )
    args = parser.parse_args()

    load_simple_env(TRACKER_ENV)
    load_simple_env(LOCAL_ENV)
    payload, payload_source = load_payload(args.source)
    state = prune_state_for_reference(
        load_state(),
        payload.get("signalReportedAt") or payload.get("lastUpdated") or now_ist_label(),
    )
    hold_labels, current_trades, next_active_trades = classify_signal_state(payload, state)
    observed_setups = build_observed_setups(
        active_trade_snapshot(payload, require_trigger=False),
        state.get("observed_setups") if isinstance(state.get("observed_setups"), dict) else {},
    )
    completed_trades_archive = build_completed_trade_archive(
        payload,
        state.get("active_trades") if isinstance(state.get("active_trades"), dict) else {},
        next_active_trades,
        state.get("completed_trades_archive") if isinstance(state.get("completed_trades_archive"), list) else [],
    )
    hold_reminder_due = bool(hold_labels) and market_is_open(payload)
    message = build_message(
        payload,
        hold_labels=hold_labels if hold_reminder_due else set(),
        active_trades=next_active_trades,
    )
    if args.print_only:
        print(message)
        return

    signature = digest_signature(payload)
    if args.record_current:
        update_status_state(
            state,
            payload=payload,
            payload_source=payload_source,
            signature=signature,
            status="Recorded current",
            detail="Current dashboard signal state was recorded without sending a Telegram message.",
            active_trades=next_active_trades,
            observed_setups=observed_setups,
            completed_trades_archive=completed_trades_archive,
            candidates=current_trades,
        )
        save_state(state)
        print(f"Recorded Telegram signal digest at {payload['lastUpdated']} from {payload_source}; no send.")
        return

    if not is_telegram_send_window():
        update_status_state(
            state,
            payload=payload,
            payload_source=payload_source,
            signature=signature,
            status="Outside send window",
            detail="Telegram sends are limited to 09:00-16:00 IST.",
            active_trades=next_active_trades,
            observed_setups=observed_setups,
            completed_trades_archive=completed_trades_archive,
            candidates=current_trades,
        )
        save_state(state)
        print("Outside 09:00-16:00 IST send window; recorded state without sending.")
        return

    if not market_is_open(payload) and not hold_reminder_due and not args.always_send:
        update_status_state(
            state,
            payload=payload,
            payload_source=payload_source,
            signature=signature,
            status="Market closed",
            detail="Market is closed, so fresh trade alerts are not sent from this snapshot.",
            active_trades=next_active_trades,
            observed_setups=observed_setups,
            completed_trades_archive=completed_trades_archive,
            candidates=current_trades,
        )
        save_state(state)
        print("Market closed; recorded state without sending.")
        return

    if not current_trades and not hold_reminder_due and not args.always_send:
        update_status_state(
            state,
            payload=payload,
            payload_source=payload_source,
            signature=signature,
            status="No new signal",
            detail="No index has a trigger-qualified buy or sell signal, so Telegram was not sent.",
            active_trades=next_active_trades,
            observed_setups=observed_setups,
            completed_trades_archive=completed_trades_archive,
            candidates=current_trades,
        )
        save_state(state)
        print("No active buy/sell signal; recorded state without sending.")
        return

    has_saved_active_state = isinstance(state.get("active_trades"), dict) and bool(state.get("active_trades"))
    if not args.always_send and has_saved_active_state and state.get("last_signature") == signature:
        update_status_state(
            state,
            payload=payload,
            payload_source=payload_source,
            signature=signature,
            status="Duplicate skipped",
            detail="Signal action, contract, entry, stop loss, target, and confidence are unchanged.",
            active_trades=next_active_trades,
            observed_setups=observed_setups,
            completed_trades_archive=completed_trades_archive,
            candidates=current_trades,
            skipped_duplicate=True,
        )
        save_state(state)
        print("Signal digest unchanged; skipped send.")
        return

    try:
        send_telegram(message, channel="signal")
    except Exception as exc:
        update_status_state(
            state,
            payload=payload,
            payload_source=payload_source,
            signature=signature,
            status="Failed",
            detail=str(exc),
            active_trades=next_active_trades,
            observed_setups=observed_setups,
            completed_trades_archive=completed_trades_archive,
            candidates=current_trades,
            failed=True,
        )
        save_state(state)
        raise

    update_status_state(
        state,
        payload=payload,
        payload_source=payload_source,
        signature=signature,
        status="Healthy",
        detail="Telegram signal digest sent successfully.",
        active_trades=next_active_trades,
        observed_setups=observed_setups,
        completed_trades_archive=completed_trades_archive,
        candidates=current_trades,
        sent=True,
    )
    if hold_reminder_due:
        state["last_hold_reminder_at"] = payload["lastUpdated"]
    save_state(state)
    print(
        f"Sent Telegram signal digest at {payload['lastUpdated']} from {payload_source}"
        + (" with hold reminder." if hold_reminder_due else ".")
    )


if __name__ == "__main__":
    main()
