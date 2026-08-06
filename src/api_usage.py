#!/usr/bin/env python3
"""
Shared client-side API usage ledger + analytics page for the Lifted apps.

WHY CLIENT-SIDE ("the proxy"): normal Anthropic API keys cannot query the
admin Usage API, so instead every app records the exact `usage` block the
API returns on every response (input/output/cache token counts straight from
Anthropic - real measurements, not estimates; only the $ figure is computed
locally from a price table).

THE LEDGER: append-only JSONL files shared by every app under
    %APPDATA%\\LiftedPDFTools\\
`api_usage.jsonl` is written by the Python apps (serialised with an msvcrt
byte-0 lock - the CRT's O_APPEND emulation is NOT atomic across processes),
`api_usage_<x>.jsonl` by single-writer Node apps. Readers merge all of them.

SCOPING: each app's analytics page shows THAT APP's usage only. The single
exception is Stage 2, which passes tool_apps=STAGE2_TOOL_APPS and gets a
scope selector: the combined TOTAL (default), its own core processing, and
one scope per workflow tool. Nothing outside that set (e.g. ApplAI) is ever
included in Stage 2's page.

SAFETY: recording can NEVER break an API call - every write is wrapped and
failures are swallowed. Corrupt/foreign lines are skipped when reading.
"""

import json
import os
import traceback
from datetime import datetime, timedelta
from pathlib import Path

VENDOR = "LiftedPDFTools"
_APP = ["unknown"]


def ledger_path() -> Path:
    override = os.environ.get("LIFTED_API_USAGE_PATH")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    d = Path(base) / VENDOR
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / "api_usage.jsonl"


def set_app(name: str):
    _APP[0] = str(name)[:40]


# ---------------------------------------------------------------------------
# Stage 2's tool set. Stage 2's analytics page = its own core processing plus
# exactly these tools (they are the API-calling utilities on its Tools panel;
# Convert to PDF is on the panel too but makes no API calls).
# ---------------------------------------------------------------------------
STAGE2_APP = "Stage 2 Processing"
STAGE2_TOOL_APPS = ("PDF Splitter", "PDF Rotator", "AI Document Splitter",
                    "Re-check Unknowns")


# ---------------------------------------------------------------------------
# Pricing (USD per million tokens). Cache writes bill at 1.25x the input
# rate, cache reads at 0.10x. The Message Batches API bills at half price.
# ---------------------------------------------------------------------------
PRICES = [
    ("haiku-4-5", 1.00, 5.00),
    ("haiku", 1.00, 5.00),
    ("sonnet-4-6", 3.00, 15.00),
    ("sonnet-5", 3.00, 15.00),
    ("sonnet", 3.00, 15.00),
    ("opus-4-8", 15.00, 75.00),
    ("opus", 15.00, 75.00),
    ("fable", 15.00, 75.00),
]
DEFAULT_PRICE = (3.00, 15.00)


def price_for(model: str):
    m = (model or "").lower()
    for sub, pin, pout in PRICES:
        if sub in m:
            return pin, pout
    return DEFAULT_PRICE


def estimate_cost(model, in_tok, out_tok, cw=0, cr=0, batch=False) -> float:
    pin, pout = price_for(model)
    usd = ((in_tok or 0) / 1e6 * pin
           + (cw or 0) / 1e6 * pin * 1.25
           + (cr or 0) / 1e6 * pin * 0.10
           + (out_tok or 0) / 1e6 * pout)
    return usd * (0.5 if batch else 1.0)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
def _locked_append(path: Path, data: bytes):
    """Append that is safe across PROCESSES on Windows.

    The CRT emulates O_APPEND as seek-to-end + write, which races between
    processes (measured: ~17% of rows lost with 4 concurrent writers).
    Serialise writers by taking a mandatory lock on byte 0 before seeking;
    byte 0 never overlaps the appended region, it is purely a mutex.
    """
    fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_RDWR)
    locked = False
    try:
        try:
            import msvcrt
            import time as _t
            for _ in range(200):            # up to ~10 s
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    _t.sleep(0.05)
        except ImportError:                  # non-Windows: O_APPEND is atomic
            pass
        os.lseek(fd, 0, os.SEEK_END)
        os.write(fd, data)
    finally:
        if locked:
            try:
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        os.close(fd)


def record_tokens(model, in_tok, out_tok, cw=0, cr=0, batch=False, app=None):
    """Append one usage row. NEVER raises."""
    try:
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "app": (app or _APP[0])[:40],
            "model": str(model or "")[:60],
            "in": int(in_tok or 0),
            "out": int(out_tok or 0),
            "cw": int(cw or 0),
            "cr": int(cr or 0),
            "batch": 1 if batch else 0,
        }
        line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        _locked_append(ledger_path(), line)
    except Exception:
        try:
            traceback.print_exc()
        except Exception:
            pass


def record_usage(model, usage: dict, batch=False, app=None):
    """Record from an Anthropic response's `usage` block. NEVER raises."""
    try:
        usage = usage or {}
        record_tokens(
            model,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            usage.get("cache_creation_input_tokens", 0),
            usage.get("cache_read_input_tokens", 0),
            batch=batch, app=app)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# High-DPI support (Tk apps). Without process DPI awareness Windows bitmap-
# stretches the whole window on scaled displays, which is what makes the UI
# look blurry. enable_high_dpi() MUST run before tkinter.Tk() is created.
# ---------------------------------------------------------------------------
def enable_high_dpi():
    """Opt this process into per-monitor DPI awareness. Safe to call on any
    platform, more than once, or after the OS refused (no-op then)."""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    except Exception:
        pass


def tk_scale(root):
    """Sync Tk's point->pixel conversion to the real monitor DPI and return
    the UI scale factor (1.0 at 96 dpi) for sizing pixel-based geometry."""
    try:
        dpi = float(root.winfo_fpixels("1i"))
        root.tk.call("tk", "scaling", dpi / 72.0)
        return dpi / 96.0
    except Exception:
        return 1.0


# ---------------------------------------------------------------------------
# Loading + aggregation
# ---------------------------------------------------------------------------
def load_rows(app=None, apps=None):
    """Read every ledger file (shared Python one + per-app Node extras).
    app=None -> all rows; app="X" -> that app only; apps=iterable -> any of
    those apps."""
    allow = set(apps) if apps is not None else None
    rows = []
    try:
        main = ledger_path()
        files = [main] + sorted(main.parent.glob(main.stem + "_*.jsonl"))
        for p in files:
            if not p.exists():
                continue
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        if app is not None and r.get("app") != app:
                            continue
                        if allow is not None and r.get("app") not in allow:
                            continue
                        r["_dt"] = datetime.fromisoformat(r["ts"])
                        r["_cost"] = estimate_cost(
                            r.get("model"), r.get("in"), r.get("out"),
                            r.get("cw"), r.get("cr"), bool(r.get("batch")))
                        rows.append(r)
                    except Exception:
                        continue
        rows.sort(key=lambda r: r["_dt"])
    except Exception:
        traceback.print_exc()
    return rows


PERIODS = [("day", 1), ("week", 7), ("month", 30), ("lifetime", None)]
PERIOD_LABEL = {"day": "24 HOURS", "week": "7 DAYS",
                "month": "30 DAYS", "lifetime": "LIFETIME"}


def _bucket():
    return {"calls": 0, "in": 0, "out": 0, "cache": 0, "cost": 0.0}


def _add(b, r):
    b["calls"] += 1
    b["in"] += r.get("in", 0)
    b["out"] += r.get("out", 0)
    b["cache"] += (r.get("cw", 0) or 0) + (r.get("cr", 0) or 0)
    b["cost"] += r["_cost"]


def _cut(now, period):
    days = dict(PERIODS)[period]
    return (now - timedelta(days=days)) if days else None


def summarize(rows):
    """{period: bucket} for the given (pre-filtered) rows."""
    now = datetime.now()
    out = {name: _bucket() for name, _ in PERIODS}
    cuts = {name: _cut(now, name) for name, _ in PERIODS}
    for r in rows:
        for name in out:
            if cuts[name] is None or r["_dt"] >= cuts[name]:
                _add(out[name], r)
    return out


def series(rows, period):
    """Time series of spend for the chart, bucketed to suit the period:
    24h -> hourly, 7/30 days -> daily, lifetime -> daily or weekly.
    Returns {"labels": [...], "values": [...], "unit": "hour|day|week"}."""
    now = datetime.now()
    if period == "day":
        start = now - timedelta(hours=23)
        start = start.replace(minute=0, second=0, microsecond=0)
        n, step, unit = 24, timedelta(hours=1), "hour"
        keyf = lambda dt: dt.strftime("%Y-%m-%d %H")
        labf = lambda dt: dt.strftime("%H:00")
    elif period in ("week", "month"):
        n = 7 if period == "week" else 30
        start = (now - timedelta(days=n - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        step, unit = timedelta(days=1), "day"
        keyf = lambda dt: dt.strftime("%Y-%m-%d")
        labf = lambda dt: dt.strftime("%d %b") if n == 7 else dt.strftime("%d")
    else:  # lifetime
        first = rows[0]["_dt"] if rows else now
        span = max(1, (now - first).days + 1)
        if span <= 35:
            n = max(7, span)
            start = (now - timedelta(days=n - 1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            step, unit = timedelta(days=1), "day"
            keyf = lambda dt: dt.strftime("%Y-%m-%d")
            labf = lambda dt: dt.strftime("%d %b")
        else:
            n = min(52, span // 7 + 1)
            start = (now - timedelta(weeks=n - 1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            start -= timedelta(days=start.weekday())
            step, unit = timedelta(weeks=1), "week"
            keyf = lambda dt: (dt - timedelta(days=dt.weekday())
                               ).strftime("%Y-%m-%d")
            labf = lambda dt: dt.strftime("%d %b")
    totals = {}
    for r in rows:
        if r["_dt"] >= start:
            totals[keyf(r["_dt"])] = totals.get(keyf(r["_dt"]), 0.0) + r["_cost"]
    labels, values = [], []
    cur = start
    for _ in range(n):
        labels.append(labf(cur))
        values.append(round(totals.get(keyf(cur), 0.0), 6))
        cur += step
    return {"labels": labels, "values": values, "unit": unit}


def by_model(rows, period):
    now = datetime.now()
    cut = _cut(now, period)
    out = {}
    for r in rows:
        if cut is None or r["_dt"] >= cut:
            b = out.setdefault(r.get("model") or "unknown", _bucket())
            _add(b, r)
    return sorted(out.items(), key=lambda kv: -kv[1]["cost"])


def scope_table(rows, members):
    """{member: {period: bucket}} for Stage 2's BY TOOL table, zero-prefilled
    so every tool appears even before its first recorded call, plus a
    __TOTAL__ pseudo-member summing them."""
    now = datetime.now()
    cuts = {name: _cut(now, name) for name, _ in PERIODS}
    out = {m: {name: _bucket() for name, _ in PERIODS} for m in members}
    total = {name: _bucket() for name, _ in PERIODS}
    for r in rows:
        ab = out.get(r.get("app"))
        if ab is None:
            continue
        for name in cuts:
            if cuts[name] is None or r["_dt"] >= cuts[name]:
                _add(ab[name], r)
                _add(total[name], r)
    out["__TOTAL__"] = total
    return out


def _dataset(rows):
    return {
        "summary": summarize(rows),
        "series": {p: series(rows, p) for p, _ in PERIODS},
        "by_model": {p: [[m, b] for m, b in by_model(rows, p)]
                     for p, _ in PERIODS},
    }


def page_data(app_name: str, include_all_apps: bool = False, tool_apps=None):
    """Everything an analytics page needs, JSON-serialisable.

    Default: scoped to app_name only. Stage 2 passes tool_apps (its core +
    each workflow tool) and additionally gets per-scope datasets in
    "scopes" (keys: "__TOTAL__", the core app name, each tool name) plus a
    "by_tool" table; the top-level summary/series/by_model then mirror the
    TOTAL scope. include_all_apps is a legacy alias for the Stage 2 set.
    """
    if tool_apps is None and include_all_apps:
        tool_apps = list(STAGE2_TOOL_APPS)
    if tool_apps:
        members = [app_name] + [t for t in tool_apps if t != app_name]
        rows = load_rows(apps=members)
        per_app = {m: [] for m in members}
        for r in rows:
            per_app[r.get("app")].append(r)
        scopes = {"__TOTAL__": _dataset(rows)}
        for m in members:
            scopes[m] = _dataset(per_app[m])
        data = _dataset(rows)
        data.update({
            "app": app_name,
            "tools": [t for t in tool_apps if t != app_name],
            "scopes": scopes,
            "by_tool": scope_table(rows, members),
            "since": rows[0]["ts"][:10] if rows else "",
            "rows": len(rows),
            "ledger": str(ledger_path().parent),
        })
        return data
    rows = load_rows(app_name)
    data = _dataset(rows)
    data.update({
        "app": app_name,
        "since": rows[0]["ts"][:10] if rows else "",
        "rows": len(rows),
        "ledger": str(ledger_path().parent),
    })
    return data


# ---------------------------------------------------------------------------
# Tk ANALYTICS WINDOW (shared by the Tkinter apps)
# ---------------------------------------------------------------------------
C_BG, C_PANEL, C_PANEL2 = "#0d0f12", "#161a20", "#1d232b"
C_BORDER, C_GRID = "#2a323d", "#232b35"
C_FG, C_DIM = "#e8eaed", "#9aa4b0"
C_MINT, C_MINT_D, C_FILL = "#3ddc97", "#1f9d55", "#12362a"
C_RIBBON = "#000000"


def _rgb(hexcol):
    hexcol = hexcol.lstrip("#")
    return tuple(int(hexcol[i:i + 2], 16) for i in (0, 2, 4))


def _fmt_usd(v):
    if v >= 100:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:,.2f}"
    if v >= 0.01:
        return f"${v:,.3f}"
    return f"${v:,.4f}" if v > 0 else "$0.00"


def _fmt_tok(v):
    if v >= 10_000_000:
        return f"{v/1e6:.0f}M"
    if v >= 1_000_000:
        return f"{v/1e6:.1f}M"
    if v >= 10_000:
        return f"{v/1e3:.0f}k"
    return f"{v:,}"


# ---------------------------------------------------------------------------
# Chart rendering. Tk's canvas has no antialiasing, so the chart is drawn
# with Pillow at 3x supersampling and LANCZOS-downscaled - smooth monotone-
# cubic line with a gradient fill, or soft rounded bars. Falls back to plain
# canvas vectors if Pillow is unavailable in the host app.
# ---------------------------------------------------------------------------
def _monotone_curve(pts, step_px):
    """Fritsch-Carlson monotone cubic through pts [(x,y)...]: smooth but
    never overshoots (no dips below zero between quiet buckets)."""
    n = len(pts)
    if n < 3:
        return list(pts)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    d = [(ys[i + 1] - ys[i]) / max(1e-9, xs[i + 1] - xs[i])
         for i in range(n - 1)]
    m = [0.0] * n
    m[0], m[-1] = d[0], d[-1]
    for i in range(1, n - 1):
        m[i] = 0.0 if d[i - 1] * d[i] <= 0 else (d[i - 1] + d[i]) / 2.0
    for i in range(n - 1):
        if d[i] == 0:
            m[i] = m[i + 1] = 0.0
            continue
        a, b = m[i] / d[i], m[i + 1] / d[i]
        k = a * a + b * b
        if k > 9.0:
            t = 3.0 / (k ** 0.5)
            m[i], m[i + 1] = t * a * d[i], t * b * d[i]
    out = []
    for i in range(n - 1):
        h = xs[i + 1] - xs[i]
        steps = max(2, int(h / max(1.0, step_px)))
        for sidx in range(steps):
            t = sidx / steps
            h00 = (1 + 2 * t) * (1 - t) ** 2
            h10 = t * (1 - t) ** 2
            h01 = t * t * (3 - 2 * t)
            h11 = t * t * (t - 1)
            out.append((xs[i] + t * h,
                        h00 * ys[i] + h10 * h * m[i]
                        + h01 * ys[i + 1] + h11 * h * m[i + 1]))
    out.append(pts[-1])
    return out


def render_chart_image(sdata, w, h, kind="line", dpi=96.0):
    """Render the spend chart to a PIL RGB Image of exactly (w, h) physical
    pixels. Returns None if Pillow is missing (caller falls back to canvas).
    """
    try:
        from PIL import Image, ImageChops, ImageDraw, ImageFont
    except Exception:
        return None
    vals, labels = sdata["values"], sdata["labels"]
    n = max(1, len(vals))
    k = max(0.75, float(dpi) / 96.0)
    SS = 3
    u = k * SS                              # logical px -> hi-res px
    W, H = int(w) * SS, int(h) * SS
    mint, dim, fgc = _rgb(C_MINT), _rgb(C_DIM), _rgb(C_FG)
    grid, panel, panel2 = _rgb(C_GRID), _rgb(C_PANEL), _rgb(C_PANEL2)
    img = Image.new("RGBA", (W, H), panel + (255,))
    dr = ImageDraw.Draw(img, "RGBA")

    def font(px, bold=False):
        name = "seguisb.ttf" if bold else "segoeui.ttf"
        try:
            return ImageFont.truetype("C:/Windows/Fonts/" + name,
                                      max(8, int(px * u)))
        except Exception:
            return ImageFont.load_default()

    f_axis, f_peak = font(11.5), font(12, bold=True)
    padl, padr = int(56 * u), int(18 * u)
    padt, padb = int(16 * u), int(30 * u)
    pw, ph = W - padl - padr, H - padt - padb
    base_y = padt + ph
    mx = max(vals) or 1.0

    # horizontal gridlines: quarters faint, 0/50/100% labelled
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = padt + ph * (1 - frac)
        a = 255 if frac in (0.0, 0.5, 1.0) else 110
        dr.line([(padl, y), (W - padr, y)], fill=grid + (a,),
                width=max(1, int(1 * u)))
        if frac in (0.0, 0.5, 1.0):
            dr.text((padl - 10 * u, y), _fmt_usd(mx * frac),
                    font=f_axis, fill=dim + (255,), anchor="rm")

    def x_at(i):
        if kind == "line":
            return padl + (pw * i / (n - 1) if n > 1 else pw / 2)
        return padl + pw * (i + 0.5) / n

    def y_at(v):
        return base_y - ph * (v / mx)

    # sparse x labels
    stride = max(1, n // 8)
    for i in range(0, n, stride):
        dr.text((x_at(i), H - padb + 14 * u), labels[i],
                font=f_axis, fill=dim + (255,), anchor="mm")

    if max(vals) <= 0:
        dr.text((padl + pw / 2, padt + ph / 2), "no usage in this period yet",
                font=font(12.5), fill=dim + (255,), anchor="mm")
        return img.convert("RGB").resize((int(w), int(h)), Image.LANCZOS)

    if kind == "bar":
        slot = pw / n
        bw = max(2 * u, slot * 0.55)
        rad = min(bw / 2, 4 * u)
        for i, v in enumerate(vals):
            xc = x_at(i)
            x0, x1 = int(xc - bw / 2), int(xc + bw / 2)
            if v <= 0:
                dr.rectangle([x0, base_y - 2 * u, x1, base_y],
                             fill=panel2 + (255,))
                continue
            y0 = int(min(y_at(v), base_y - 2 * u))
            bh = max(1, int(base_y) - y0)
            # vertical gradient, rounded top corners only
            bar = Image.new("RGBA", (max(1, x1 - x0), bh))
            bd = ImageDraw.Draw(bar)
            c_top, c_bot = (92, 232, 171), (34, 184, 119)
            for yy in range(bh):
                t = yy / max(1, bh - 1)
                col = tuple(int(c_top[ci] + (c_bot[ci] - c_top[ci]) * t)
                            for ci in range(3)) + (255,)
                bd.line([(0, yy), (bar.width, yy)], fill=col)
            mask = Image.new("L", bar.size, 0)
            md = ImageDraw.Draw(mask)
            r = int(min(rad, bh / 2))
            md.rounded_rectangle([0, 0, bar.width - 1, bh - 1],
                                 radius=r, fill=255)
            md.rectangle([0, r, bar.width - 1, bh - 1], fill=255)
            img.paste(bar, (x0, y0), mask)
        pi = vals.index(max(vals))
        dr.text((x_at(pi), max(y_at(vals[pi]) - 14 * u, padt + 8 * u)),
                _fmt_usd(vals[pi]), font=f_peak, fill=fgc + (255,),
                anchor="mm")
    else:
        pts = [(x_at(i), y_at(v)) for i, v in enumerate(vals)]
        curve = _monotone_curve(pts, step_px=3 * u)
        curve = [(x, min(max(y, padt), base_y)) for x, y in curve]
        # gradient area fill under the curve
        poly = [(padl, base_y)] + curve + [(W - padr, base_y)]
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(layer).polygon(poly, fill=mint + (255,))
        top_y = int(min(y for _, y in curve))
        ramp = Image.new("L", (1, H), 0)
        rpx = ramp.load()
        denom = max(1, int(base_y) - top_y)
        for yy in range(H):
            if yy >= base_y:
                rpx[0, yy] = 0
            elif yy <= top_y:
                rpx[0, yy] = 88
            else:
                rpx[0, yy] = int(88 * (base_y - yy) / denom)
        layer.putalpha(ImageChops.multiply(layer.getchannel("A"),
                                           ramp.resize((W, H))))
        img.alpha_composite(layer)
        # soft glow + crisp line
        dr.line(curve, fill=mint + (42,), width=int(7 * u), joint="curve")
        dr.line(curve, fill=mint + (255,), width=max(2, int(2.4 * u)),
                joint="curve")
        if n <= 32:
            r_in, r_out = 3.2 * u, 4.8 * u
            for (x, y), v in zip(pts, vals):
                dr.ellipse([x - r_out, y - r_out, x + r_out, y + r_out],
                           fill=panel + (255,))
                dr.ellipse([x - r_in, y - r_in, x + r_in, y + r_in],
                           fill=mint + (255,))
        pi = vals.index(max(vals))
        px_, py_ = pts[pi]
        dr.text((min(max(px_, padl + 26 * u), W - padr - 30 * u),
                 max(py_ - 16 * u, padt + 8 * u)),
                _fmt_usd(vals[pi]), font=f_peak, fill=fgc + (255,),
                anchor="mm")

    return img.convert("RGB").resize((int(w), int(h)), Image.LANCZOS)


def open_analytics_window(parent, app_name: str, include_all_apps=False,
                          tool_apps=None):
    """Open the analytics page as a Toplevel. Never raises.
    tool_apps (Stage 2 only): list of tool app names -> the window gets a
    scope selector (Total / Core Processing / one per tool)."""
    try:
        if tool_apps is None and include_all_apps:
            tool_apps = list(STAGE2_TOOL_APPS)
        return _AnalyticsWindow(parent, app_name, tool_apps)
    except Exception:
        traceback.print_exc()


class _AnalyticsWindow:
    TOTAL = "__TOTAL__"

    def __init__(self, parent, app_name, tool_apps=None):
        import tkinter as tk
        self.tk = tk
        self.app_name = app_name
        self.tool_apps = [t for t in (tool_apps or []) if t != app_name]
        self.scope = self.TOTAL if self.tool_apps else app_name
        self.period = "week"
        self.chart_kind = "line"
        self.data = None
        self._resize_after = None
        self._chart_photo = None

        win = tk.Toplevel(parent)
        self.win = win
        win.title(("API Usage — Stage 2 & Tools" if self.tool_apps
                   else f"API Analytics — {app_name}"))
        win.configure(bg=C_BG)
        self._ui = tk_scale(win)
        s = self._ui

        def px(v):
            return int(round(v * s))

        self._px = px
        gw, gh = (1080, 880) if self.tool_apps else (960, 780)
        win.geometry(f"{px(gw)}x{px(gh)}")
        win.minsize(px(880), px(660))
        try:
            import ctypes
            win.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
            v = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(v), ctypes.sizeof(v))
        except Exception:
            pass

        # font families: Segoe UI Variable when the OS has it (Win 11)
        self.FU = self._family("Segoe UI Variable Text", "Segoe UI")
        self.FD = self._family("Segoe UI Variable Display",
                               "Segoe UI Variable Text", "Segoe UI")

        hdr = tk.Frame(win, bg=C_RIBBON, height=px(54))
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="📊  API Analytics", bg=C_RIBBON, fg="#ffffff",
                 font=(self.FD, 14, "bold"), padx=px(16)).pack(side="left")
        sub = ("core processing + workflow tools" if self.tool_apps
               else "this app only")
        tk.Label(hdr, text=f"{app_name} · {sub} · exact token counts from "
                           f"every API response",
                 bg=C_RIBBON, fg="#8a8f98", font=(self.FU, 9))\
            .pack(side="left")
        tk.Button(hdr, text="⟳  Refresh", command=self.reload,
                  bg="#141414", fg="#bbbbbb", relief="flat", bd=0,
                  padx=px(12), font=(self.FU, 10), cursor="hand2",
                  activebackground="#1a1a1a", activeforeground="#ffffff")\
            .pack(side="right", padx=px(10), pady=px(9))

        # ---- control rows: scope (Stage 2 only) + period ----
        ctl = tk.Frame(win, bg=C_BG)
        ctl.pack(fill="x", padx=px(18), pady=(px(12), px(2)))
        self._scope_btns = {}
        if self.tool_apps:
            row = tk.Frame(ctl, bg=C_BG)
            row.pack(fill="x", pady=(0, px(6)))
            tk.Label(row, text="SCOPE", bg=C_BG, fg=C_DIM, width=7,
                     anchor="w", font=(self.FU, 8, "bold")).pack(side="left")
            for key, label in self._scope_list():
                b = tk.Button(row, text=label,
                              command=lambda kk=key: self.set_scope(kk),
                              relief="flat", bd=0, font=(self.FU, 9, "bold"),
                              padx=px(13), pady=px(5), cursor="hand2")
                b.pack(side="left", padx=(0, px(5)))
                self._scope_btns[key] = b
        prow = tk.Frame(ctl, bg=C_BG)
        prow.pack(fill="x")
        if self.tool_apps:
            tk.Label(prow, text="PERIOD", bg=C_BG, fg=C_DIM, width=7,
                     anchor="w", font=(self.FU, 8, "bold")).pack(side="left")
        self._seg_btns = {}
        for p, _ in PERIODS:
            b = tk.Button(prow, text=PERIOD_LABEL[p],
                          command=lambda pp=p: self.set_period(pp),
                          relief="flat", bd=0, font=(self.FU, 10, "bold"),
                          padx=px(18), pady=px(7), cursor="hand2")
            b.pack(side="left", padx=(0, px(6)))
            self._seg_btns[p] = b

        self.body = tk.Frame(win, bg=C_BG)
        self.body.pack(fill="both", expand=True, padx=px(18), pady=px(6))

        self.foot = tk.Label(win, text="", bg=C_PANEL2, fg=C_DIM,
                             font=(self.FU, 9), anchor="w", padx=px(12),
                             pady=px(5))
        self.foot.pack(fill="x", side="bottom")
        self.reload()

    # ------------------------------------------------------------------
    def _family(self, *prefs):
        try:
            import tkinter.font as tkfont
            fams = set(tkfont.families(self.win))
            for p in prefs:
                if p in fams:
                    return p
        except Exception:
            pass
        return "Segoe UI"

    def _scope_list(self):
        return ([(self.TOTAL, "Total"), (self.app_name, "Core Processing")]
                + [(t, t) for t in self.tool_apps])

    def _scope_label(self):
        if self.scope == self.TOTAL:
            return f"TOTAL · CORE + {len(self.tool_apps)} TOOLS"
        if self.scope == self.app_name and self.tool_apps:
            return "CORE PROCESSING"
        return self.scope.upper()

    def _dataset(self):
        d = self.data
        if self.tool_apps:
            return d["scopes"].get(self.scope) or d["scopes"][self.TOTAL]
        return d

    # ------------------------------------------------------------------
    def reload(self):
        self.data = page_data(self.app_name,
                              tool_apps=self.tool_apps or None)
        self.render()

    def set_period(self, p):
        self.period = p
        self.render()

    def set_scope(self, sc):
        self.scope = sc
        self.render()

    def toggle_chart(self, kind):
        self.chart_kind = kind
        self.render()

    # ------------------------------------------------------------------
    def render(self):
        tk = self.tk
        px = self._px
        d = self.data
        ds = self._dataset()
        for p, b in self._seg_btns.items():
            if p == self.period:
                b.configure(bg=C_MINT, fg="#000000",
                            activebackground=C_MINT_D,
                            activeforeground="#000000")
            else:
                b.configure(bg=C_PANEL2, fg=C_DIM,
                            activebackground=C_BORDER,
                            activeforeground=C_FG)
        for sc, b in self._scope_btns.items():
            if sc == self.scope:
                b.configure(bg=C_MINT, fg="#000000",
                            activebackground=C_MINT_D,
                            activeforeground="#000000")
            else:
                b.configure(bg=C_PANEL2, fg=C_DIM,
                            activebackground=C_BORDER,
                            activeforeground=C_FG)
        for w in self.body.winfo_children():
            w.destroy()

        # ---- hero total for the selected scope + period ----
        bkt = ds["summary"][self.period]
        hero = tk.Frame(self.body, bg=C_PANEL)
        hero.pack(fill="x")
        left = tk.Frame(hero, bg=C_PANEL)
        left.pack(side="left", padx=px(18), pady=px(14))
        scope_txt = (self._scope_label() if self.tool_apps
                     else self.app_name.upper())
        tk.Label(left, text=f"{PERIOD_LABEL[self.period]} · {scope_txt}",
                 bg=C_PANEL, fg=C_DIM, font=(self.FU, 9, "bold"))\
            .pack(anchor="w")
        tk.Label(left, text=_fmt_usd(bkt["cost"]), bg=C_PANEL, fg=C_MINT,
                 font=(self.FD, 35, "bold")).pack(anchor="w")
        right = tk.Frame(hero, bg=C_PANEL)
        right.pack(side="right", padx=px(18), pady=px(14))
        for label, val in (("calls", f"{bkt['calls']:,}"),
                           ("tokens in", _fmt_tok(bkt["in"])),
                           ("tokens out", _fmt_tok(bkt["out"])),
                           ("cached", _fmt_tok(bkt["cache"]))):
            cell = tk.Frame(right, bg=C_PANEL)
            cell.pack(side="left", padx=px(12))
            tk.Label(cell, text=val, bg=C_PANEL, fg=C_FG,
                     font=(self.FD, 15, "bold")).pack()
            tk.Label(cell, text=label, bg=C_PANEL, fg=C_DIM,
                     font=(self.FU, 8)).pack()

        # ---- chart ----
        card = tk.Frame(self.body, bg=C_PANEL)
        card.pack(fill="x", pady=(px(10), 0))
        chead = tk.Frame(card, bg=C_PANEL)
        chead.pack(fill="x", padx=px(14), pady=(px(10), px(2)))
        unit = ds["series"][self.period]["unit"]
        tk.Label(chead, text=f"SPEND — {PERIOD_LABEL[self.period]} "
                             f"(per {unit})",
                 bg=C_PANEL, fg=C_DIM, font=(self.FU, 9, "bold"))\
            .pack(side="left")
        for kind, label in (("line", "📈 Line"), ("bar", "📊 Bars")):
            on = kind == self.chart_kind
            tk.Button(chead, text=label,
                      command=lambda k=kind: self.toggle_chart(k),
                      bg=C_MINT if on else C_PANEL2,
                      fg="#000000" if on else C_DIM,
                      activebackground=C_MINT_D if on else C_BORDER,
                      activeforeground="#000000" if on else C_FG,
                      relief="flat", bd=0, font=(self.FU, 9, "bold"),
                      padx=px(12), pady=px(3), cursor="hand2")\
                .pack(side="right", padx=(px(6), 0))
        self.canvas = tk.Canvas(card, bg=C_PANEL, height=px(235),
                                highlightthickness=0)
        self.canvas.pack(fill="x", padx=px(14), pady=(px(4), px(12)))
        self.canvas.bind("<Configure>", self._on_resize)
        self.win.after(30, self.draw_chart)

        # ---- tables ----
        tables = tk.Frame(self.body, bg=C_BG)
        tables.pack(fill="both", expand=True, pady=(px(10), 0))
        show_tools = bool(self.tool_apps) and self.scope == self.TOTAL
        if show_tools:
            tables.columnconfigure(0, weight=3)
            tables.columnconfigure(1, weight=2)
            self._table_by_tool(tables, 0)
            self._table_by_model(tables, 1)
        else:
            tables.columnconfigure(0, weight=1)
            self._table_by_model(tables, 0)

        self.foot.configure(
            text=f"{d['rows']:,} recorded calls since {d['since'] or '—'} · "
                 f"ledger: {d['ledger']} · cost estimated in USD, batch "
                 f"calls at 50%")

    # ------------------------------------------------------------------
    def _on_resize(self, _e):
        if self._resize_after:
            try:
                self.win.after_cancel(self._resize_after)
            except Exception:
                pass
        self._resize_after = self.win.after(120, self.draw_chart)

    def draw_chart(self):
        cv = self.canvas
        try:
            cv.delete("all")
        except Exception:
            return
        s = self._dataset()["series"][self.period]
        w = max(cv.winfo_width(), 300)
        h = max(cv.winfo_height(), 120)
        try:
            dpi = float(self.win.winfo_fpixels("1i"))
        except Exception:
            dpi = 96.0
        img = render_chart_image(s, w, h, self.chart_kind, dpi=dpi)
        if img is not None:
            try:
                from PIL import ImageTk
                self._chart_photo = ImageTk.PhotoImage(img, master=cv)
                cv.create_image(0, 0, anchor="nw", image=self._chart_photo)
                return
            except Exception:
                pass
        self._draw_chart_canvas(cv, s, w, h)

    def _draw_chart_canvas(self, cv, s, W, H):
        """Vector fallback when Pillow is unavailable."""
        vals, labels = s["values"], s["labels"]
        padl, padr, padt, padb = 46, 14, 16, 26
        pw, ph = W - padl - padr, H - padt - padb
        mx = max(vals) or 1.0
        n = len(vals)
        for frac in (0.0, 0.5, 1.0):
            y = padt + ph * (1 - frac)
            cv.create_line(padl, y, W - padr, y, fill=C_GRID)
            cv.create_text(padl - 8, y, text=_fmt_usd(mx * frac),
                           fill=C_DIM, font=(self.FU, 8), anchor="e")

        def x_at(i):
            return padl + (pw * i / max(1, n - 1) if self.chart_kind == "line"
                           else pw * (i + 0.5) / n)

        def y_at(v):
            return padt + ph * (1 - v / mx)

        stride = max(1, n // 8)
        for i in range(0, n, stride):
            cv.create_text(x_at(i), H - padb + 12, text=labels[i],
                           fill=C_DIM, font=(self.FU, 8))
        if self.chart_kind == "bar":
            bw = max(3, int(pw / n * 0.55))
            for i, v in enumerate(vals):
                x = x_at(i)
                if v <= 0:
                    cv.create_rectangle(x - bw / 2, padt + ph - 2,
                                        x + bw / 2, padt + ph,
                                        fill=C_PANEL2, width=0)
                    continue
                cv.create_rectangle(x - bw / 2, y_at(v), x + bw / 2,
                                    padt + ph, fill=C_MINT, width=0)
        else:
            pts = [(x_at(i), y_at(v)) for i, v in enumerate(vals)]
            poly = [(padl, padt + ph)] + pts + [(W - padr, padt + ph)]
            cv.create_polygon(*[c for p in poly for c in p],
                              fill=C_FILL, outline="")
            cv.create_line(*[c for p in pts for c in p], fill=C_MINT,
                           width=2, smooth=True)
            if n <= 32:
                for (x, y), v in zip(pts, vals):
                    cv.create_oval(x - 3, y - 3, x + 3, y + 3,
                                   fill=C_MINT, outline=C_BG)
        if max(vals) > 0:
            pi = vals.index(max(vals))
            cv.create_text(min(max(x_at(pi), padl + 20), W - padr - 24),
                           max(y_at(vals[pi]) - 12, padt + 6),
                           text=_fmt_usd(vals[pi]), fill=C_FG,
                           font=(self.FU, 8, "bold"))

    # ------------------------------------------------------------------
    def _table_by_model(self, parent, col):
        tk = self.tk
        px = self._px
        t = tk.Frame(parent, bg=C_PANEL)
        t.grid(row=0, column=col, sticky="nsew",
               padx=(px(6), 0) if col else (0, 0))
        scope_txt = f" — {self._scope_label()}" if self.tool_apps else ""
        tk.Label(t, text=f"BY MODEL — {PERIOD_LABEL[self.period]}{scope_txt}",
                 bg=C_PANEL, fg=C_DIM, font=(self.FU, 9, "bold"),
                 padx=px(14), pady=px(8)).pack(anchor="w")
        tbl = tk.Frame(t, bg=C_PANEL)
        tbl.pack(fill="x", padx=px(14), pady=(0, px(12)))
        for c, htxt in enumerate(["Model", "Calls", "Tokens in/out", "Cost"]):
            tk.Label(tbl, text=htxt, bg=C_PANEL, fg=C_DIM,
                     font=(self.FU, 9, "bold"), anchor="w")\
                .grid(row=0, column=c, sticky="w", padx=px(6), pady=px(2))
            tbl.columnconfigure(c, weight=2 if c == 0 else 1)
        rows = self._dataset()["by_model"][self.period]
        if not rows:
            tk.Label(tbl, text="no usage in this period", bg=C_PANEL,
                     fg=C_DIM, font=(self.FU, 10))\
                .grid(row=1, column=0, columnspan=4, sticky="w", padx=px(6))
        for rix, (mdl, b) in enumerate(rows, 1):
            for c, txt in enumerate((mdl.replace("claude-", ""),
                                     f"{b['calls']:,}",
                                     f"{_fmt_tok(b['in'])} / {_fmt_tok(b['out'])}",
                                     _fmt_usd(b["cost"]))):
                tk.Label(tbl, text=txt, bg=C_PANEL,
                         fg=C_MINT if c == 3 else C_FG,
                         font=(self.FU, 10), anchor="w")\
                    .grid(row=rix, column=c, sticky="w", padx=px(6),
                          pady=px(1))

    def _table_by_tool(self, parent, col):
        """Stage 2, Total scope: per-tool costs across all periods. Rows are
        clickable and jump to that tool's scope."""
        tk = self.tk
        px = self._px
        t = tk.Frame(parent, bg=C_PANEL)
        t.grid(row=0, column=col, sticky="nsew", padx=(0, px(6)))
        head = tk.Frame(t, bg=C_PANEL)
        head.pack(fill="x")
        tk.Label(head, text="BY TOOL — core + workflow tools", bg=C_PANEL,
                 fg=C_DIM, font=(self.FU, 9, "bold"), padx=px(14),
                 pady=px(8)).pack(side="left")
        tk.Label(head, text="click a row to focus it", bg=C_PANEL,
                 fg="#5c6672", font=(self.FU, 8), padx=px(6))\
            .pack(side="left")
        tbl = tk.Frame(t, bg=C_PANEL)
        tbl.pack(fill="x", padx=px(14), pady=(0, px(12)))
        heads = ["Tool", "24h", "7 days", "30 days", "Lifetime"]
        for c, htxt in enumerate(heads):
            tk.Label(tbl, text=htxt, bg=C_PANEL, fg=C_DIM,
                     font=(self.FU, 9, "bold"), anchor="w")\
                .grid(row=0, column=c, sticky="w", padx=px(6), pady=px(2))
            tbl.columnconfigure(c, weight=2 if c == 0 else 1)
        by_tool = self.data.get("by_tool") or {}
        total = by_tool.get(self.TOTAL)
        members = ([(self.app_name, "Core Processing")]
                   + [(tn, tn) for tn in self.tool_apps])
        for rix, (key, label) in enumerate(members, 1):
            per = by_tool.get(key) or {p: _bucket() for p, _ in PERIODS}
            lbl = tk.Label(tbl, text=label, bg=C_PANEL, fg=C_FG,
                           font=(self.FU, 10), anchor="w", cursor="hand2")
            lbl.grid(row=rix, column=0, sticky="w", padx=px(6))
            lbl.bind("<Button-1>", lambda _e, kk=key: self.set_scope(kk))
            lbl.bind("<Enter>", lambda _e, L=lbl: L.configure(fg=C_MINT))
            lbl.bind("<Leave>", lambda _e, L=lbl: L.configure(fg=C_FG))
            for c, p in enumerate(("day", "week", "month", "lifetime"), 1):
                cost = per[p]["cost"]
                tk.Label(tbl, text=_fmt_usd(cost), bg=C_PANEL,
                         fg=C_FG if cost > 0 else "#5c6672",
                         font=(self.FU, 10), anchor="w")\
                    .grid(row=rix, column=c, sticky="w", padx=px(6))
        if total:
            rix = len(members) + 1
            tk.Label(tbl, text="TOTAL", bg=C_PANEL, fg=C_MINT,
                     font=(self.FU, 10, "bold"), anchor="w")\
                .grid(row=rix, column=0, sticky="w", padx=px(6),
                      pady=(px(4), 0))
            for c, p in enumerate(("day", "week", "month", "lifetime"), 1):
                tk.Label(tbl, text=_fmt_usd(total[p]["cost"]), bg=C_PANEL,
                         fg=C_MINT, font=(self.FU, 10, "bold"), anchor="w")\
                    .grid(row=rix, column=c, sticky="w", padx=px(6),
                          pady=(px(4), 0))
