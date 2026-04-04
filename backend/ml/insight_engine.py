from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any


def _safe_hour_label(hour: int) -> str:
    if hour == 0:
        return "12 AM"
    if hour == 12:
        return "12 PM"
    if hour > 12:
        return f"{hour - 12} PM"
    return f"{hour} AM"


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def analyze_focus_patterns(email: str, activities) -> dict[str, Any]:
    logs = list(
        activities.find(
            {"email": email},
            {"_id": 0, "timestamp": 1, "type": 1, "duration": 1, "app": 1},
        )
    )

    if not logs:
        return {
            "best_focus_hours": [],
            "summary": "Not enough activity data yet. Keep the tracker running and the system will learn your real focus windows.",
            "confidence": "low",
            "sample_size": 0,
        }

    hourly = defaultdict(
        lambda: {
            "productive_minutes": 0,
            "distracting_minutes": 0,
            "neutral_minutes": 0,
            "entries": 0,
            "days": set(),
            "productive_entries": 0,
        }
    )

    total_productive = 0
    total_tracked = 0
    active_days: set[Any] = set()
    recent_logs = []
    now = datetime.now()
    recent_cutoff = now - timedelta(days=14)

    for log in logs:
        timestamp = log.get("timestamp")
        if not isinstance(timestamp, datetime):
            continue

        hour = int(timestamp.hour)
        duration = max(int(log.get("duration") or 0), 0)
        log_type = str(log.get("type") or "neutral").strip().lower()
        day_key = timestamp.date().isoformat()

        bucket = hourly[hour]
        bucket["entries"] += 1
        bucket["days"].add(day_key)
        active_days.add(day_key)
        total_tracked += duration

        if timestamp >= recent_cutoff:
            recent_logs.append(log)

        if log_type == "productive":
            bucket["productive_minutes"] += duration
            bucket["productive_entries"] += 1
            total_productive += duration
        elif log_type == "distracting":
            bucket["distracting_minutes"] += duration
        else:
            bucket["neutral_minutes"] += duration

    scored_hours = []
    day_count = max(len(active_days), 1)
    overall_ratio = _safe_ratio(total_productive, total_tracked)

    for hour, data in hourly.items():
        productive = data["productive_minutes"]
        distracting = data["distracting_minutes"]
        neutral = data["neutral_minutes"]
        tracked = productive + distracting + neutral
        if tracked <= 0:
            continue

        focus_ratio = _safe_ratio(productive, tracked)
        distraction_ratio = _safe_ratio(distracting, tracked)
        consistency = len(data["days"]) / day_count
        minute_weight = min(tracked / 180.0, 1.0)
        confidence_score = (
            0.45 * focus_ratio
            + 0.25 * consistency
            + 0.20 * minute_weight
            + 0.10 * (1 - distraction_ratio)
        )
        final_score = (
            0.55 * focus_ratio
            + 0.20 * consistency
            + 0.15 * minute_weight
            + 0.10 * (1 - distraction_ratio)
        )

        scored_hours.append(
            {
                "hour": hour,
                "label": _safe_hour_label(hour),
                "score": round(final_score * 100, 1),
                "focus_ratio": round(focus_ratio * 100, 1),
                "productive_minutes": productive,
                "distracting_minutes": distracting,
                "tracked_minutes": tracked,
                "consistency": round(consistency * 100, 1),
                "confidence_score": round(confidence_score * 100, 1),
                "sample_days": len(data["days"]),
                "entries": data["entries"],
            }
        )

    scored_hours.sort(
        key=lambda item: (
            item["score"],
            item["productive_minutes"],
            item["consistency"],
            item["entries"],
        ),
        reverse=True,
    )

    best_hours = scored_hours[:3]

    if len(logs) < 60 or len(active_days) < 3:
        confidence = "low"
    elif len(logs) < 250 or len(active_days) < 7:
        confidence = "medium"
    else:
        confidence = "high"

    if best_hours:
        best = best_hours[0]
        delta = max(best["focus_ratio"] - round(overall_ratio * 100, 1), 0)
        summary = (
            f"Your strongest focus window is around {best['label']}. "
            f"In that hour you are productive {best['focus_ratio']}% of the time "
            f"across {best['sample_days']} active days, which is {round(delta, 1)} points above your overall baseline."
        )
    else:
        summary = "There is not enough hour-level data yet to identify a reliable best focus window."

    recent_productive = sum(
        max(int(log.get("duration") or 0), 0)
        for log in recent_logs
        if str(log.get("type") or "").lower() == "productive"
    )
    recent_total = sum(max(int(log.get("duration") or 0), 0) for log in recent_logs)
    recent_ratio = round(_safe_ratio(recent_productive, recent_total) * 100, 1)

    return {
        "best_focus_hours": best_hours,
        "summary": summary,
        "confidence": confidence,
        "sample_size": len(logs),
        "active_days": len(active_days),
        "overall_focus_ratio": round(overall_ratio * 100, 1),
        "recent_focus_ratio": recent_ratio,
    }


def get_best_focus_hours(email, activities):
    return analyze_focus_patterns(email, activities)["best_focus_hours"]
