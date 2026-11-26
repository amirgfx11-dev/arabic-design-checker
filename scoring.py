"""Deterministic 0-100 quality score. Pure arithmetic over the findings: no model involved."""
import math

W_ERR, W_WARN = 4.0, 1.5     # points per finding
LOW_CONF = 0.5               # low-confidence findings count at half weight
CAP = 30.0                   # no single check can take more than this (before page scaling)
BANDS = [(90, "excellent", "ممتاز", "#86f282"), (75, "good", "جيد", "#c8f26b"),
         (50, "review", "يحتاج مراجعة", "#ffb454"), (0, "work", "يحتاج عملًا", "#ff6b6b")]


def band(v):
    for lo, key, ar, color in BANDS:
        if v >= lo:
            return key, ar, color
    return BANDS[-1][1:]


def compute(items, rules, pages):
    """rules: engine.RULES (key -> (en, ar)). Returns dict(value, band, band_ar, color, parts, scale, ...)."""
    by = {}
    for it in items:
        sev = it.get("severity")
        w = W_ERR if sev == "error" else W_WARN if sev == "warn" else 0
        if not w:
            continue
        if it.get("confidence") == "low":
            w *= LOW_CONF
        p = by.setdefault(it["check"], dict(check=it["check"], name=rules[it["check"]][1], errors=0, warns=0, raw=0.0))
        p["errors"] += sev == "error"
        p["warns"] += sev == "warn"
        p["raw"] += w
    scale = math.sqrt(max(1, pages))
    parts = []
    total = 0.0
    for p in by.values():
        pen = min(p["raw"], CAP) / scale
        total += pen
        parts.append(dict(check=p["check"], name=p["name"], errors=p["errors"], warns=p["warns"], penalty=round(pen, 1), capped=p["raw"] > CAP))
    parts.sort(key=lambda x: -x["penalty"])
    value = max(0, min(100, int(round(100 - total))))
    key, ar, color = band(value)
    return dict(value=value, band=key, band_ar=ar, color=color, parts=parts, scale=round(scale, 2), pages=pages,
                w_err=W_ERR, w_warn=W_WARN, cap=CAP)
