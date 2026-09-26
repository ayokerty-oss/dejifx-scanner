#!/usr/bin/env python3
"""
DejiFX Signal Scanner — ICT/SMC Setup Detector
Watches GBPUSD, GBPCHF, XAUUSD, AUDUSD, EURCHF on 1M candles.
Detects: SSL/BSL sweep → BOS → FVG → Telegram alert.
Reads credentials from environment variables (for GitHub Actions).
"""

import sys, os, urllib.request, urllib.parse, json, time
from datetime import datetime, timezone, timedelta

# ── Credentials (from env vars when running in the cloud) ────────────────────
TWELVE_DATA_KEY      = os.environ.get("TWELVE_DATA_KEY", "")
BOT_TOKEN            = os.environ.get("BOT_TOKEN", "")
CHAT_ID              = os.environ.get("CHAT_ID", "")
PAIRS                = ["GBP/USD", "GBP/CHF", "XAU/USD", "AUD/USD", "EUR/CHF"]
SESSIONS             = {"London": (2.0, 5.5), "New York": (7.0, 11.0)}
LOOKBACK             = 50
ALERT_COOLDOWN_HOURS = 4
ALERT_LOG            = "alert_log.json"

# ── Time helpers ──────────────────────────────────────────────────────────────

def nyt_now():
    utc = datetime.now(timezone.utc)
    offset = -4 if 3 < utc.month < 11 else -5
    return utc + timedelta(hours=offset)

def current_session():
    now = nyt_now()
    h = now.hour + now.minute / 60.0
    for name, (start, end) in SESSIONS.items():
        if start <= h <= end:
            return name, now
    return None, now

# ── Twelve Data ───────────────────────────────────────────────────────────────

def fetch_candles(symbol):
    url = (
        "https://api.twelvedata.com/time_series"
        f"?symbol={urllib.parse.quote(symbol)}"
        f"&interval=1min&outputsize={LOOKBACK}"
        f"&apikey={TWELVE_DATA_KEY}&format=JSON"
    )
    with urllib.request.urlopen(urllib.request.Request(url), timeout=15) as r:
        data = json.loads(r.read())
    if data.get("status") == "error":
        raise ValueError(data.get("message", "API error"))
    candles = data.get("values", [])
    candles.reverse()
    return [{"t": c["datetime"], "o": float(c["open"]), "h": float(c["high"]),
             "l": float(c["low"]), "c": float(c["close"])} for c in candles]

# ── ICT/SMC Detection ─────────────────────────────────────────────────────────

def swing_lows(candles, wing=3):
    result = []
    for i in range(wing, len(candles) - wing):
        lo = candles[i]["l"]
        if all(lo <= candles[j]["l"] for j in range(i - wing, i + wing + 1) if j != i):
            result.append(i)
    return result

def swing_highs(candles, wing=3):
    result = []
    for i in range(wing, len(candles) - wing):
        hi = candles[i]["h"]
        if all(hi >= candles[j]["h"] for j in range(i - wing, i + wing + 1) if j != i):
            result.append(i)
    return result

def find_bullish_fvg(candles):
    recent = candles[-15:]
    for i in range(len(recent) - 1, 1, -1):
        gap_bot, gap_top = recent[i - 2]["h"], recent[i]["l"]
        if gap_top > gap_bot:
            return {"low": gap_bot, "high": gap_top, "time": recent[i]["t"]}
    return None

def find_bearish_fvg(candles):
    recent = candles[-15:]
    for i in range(len(recent) - 1, 1, -1):
        gap_top, gap_bot = recent[i - 2]["l"], recent[i]["h"]
        if gap_bot < gap_top:
            return {"low": gap_bot, "high": gap_top, "time": recent[i]["t"]}
    return None

def detect_setup(candles):
    if len(candles) < 20:
        return None
    c, last = candles, candles[-1]
    sl_idx, sh_idx = swing_lows(c, wing=3), swing_highs(c, wing=3)

    if sl_idx:
        ref_sl_i = sl_idx[-1]
        ref_sl   = c[ref_sl_i]["l"]
        post     = c[ref_sl_i + 1:]
        if any(x["l"] < ref_sl for x in post[-10:]):
            pre_sh = [i for i in sh_idx if i < ref_sl_i + len(post) - 2]
            if pre_sh:
                bos = c[pre_sh[-1]]["h"]
                if last["c"] > bos:
                    pip  = 0.0001
                    stop = ref_sl - pip * 5
                    return {"bias": "bullish", "direction": "BUY", "lq_swept": "SSL",
                            "swept_level": ref_sl, "bos_level": bos, "stop": stop,
                            "target_1r3": bos + (bos - stop) * 3,
                            "fvg": find_bullish_fvg(c),
                            "entry_price": last["c"], "candle_time": last["t"]}

    if sh_idx:
        ref_sh_i = sh_idx[-1]
        ref_sh   = c[ref_sh_i]["h"]
        post     = c[ref_sh_i + 1:]
        if any(x["h"] > ref_sh for x in post[-10:]):
            pre_sl = [i for i in sl_idx if i < ref_sh_i + len(post) - 2]
            if pre_sl:
                bos = c[pre_sl[-1]]["l"]
                if last["c"] < bos:
                    pip  = 0.0001
                    stop = ref_sh + pip * 5
                    return {"bias": "bearish", "direction": "SELL", "lq_swept": "BSL",
                            "swept_level": ref_sh, "bos_level": bos, "stop": stop,
                            "target_1r3": bos - (stop - bos) * 3,
                            "fvg": find_bearish_fvg(c),
                            "entry_price": last["c"], "candle_time": last["t"]}
    return None

# ── Alert deduplication (persisted via GitHub Actions cache) ──────────────────

def load_log():
    if not os.path.exists(ALERT_LOG):
        return {}
    with open(ALERT_LOG) as f:
        return json.load(f)

def save_log(log):
    with open(ALERT_LOG, "w") as f:
        json.dump(log, f, indent=2)

def already_alerted(pair, session, nyt_dt):
    log = load_log()
    key = f"{pair}|{session}"
    if key not in log:
        return False
    last_ts = datetime.fromisoformat(log[key])
    return (nyt_dt - last_ts).total_seconds() / 3600 < ALERT_COOLDOWN_HOURS

def mark_alerted(pair, session, nyt_dt):
    log = load_log()
    log[f"{pair}|{session}"] = nyt_dt.isoformat()
    save_log(log)

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text):
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=data, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def format_alert(pair_display, session, setup, nyt_dt):
    emoji = "🟢" if setup["bias"] == "bullish" else "🔴"
    dec   = 2 if "XAU" in pair_display else 5
    fvg   = setup["fvg"]
    fvg_line = (
        f"FVG zone : {fvg['low']:.{dec}f} - {fvg['high']:.{dec}f}"
        if fvg else "FVG      : watch next retrace"
    )
    return (
        f"{emoji} DejiFX Signal\n\n"
        f"Pair     : {pair_display}\n"
        f"Session  : {session}\n"
        f"Signal   : {setup['direction']}\n"
        f"Setup    : {setup['lq_swept']} Sweep -> BOS -> FVG\n\n"
        f"Swept at : {setup['swept_level']:.{dec}f}\n"
        f"BOS at   : {setup['bos_level']:.{dec}f}\n"
        f"{fvg_line}\n"
        f"Stop     : {setup['stop']:.{dec}f}\n"
        f"TP (1:3) : {setup['target_1r3']:.{dec}f}\n\n"
        f"Time     : {nyt_dt.strftime('%H:%M')} NYT\n"
        f"Candle   : {setup['candle_time']}\n\n"
        f"Always confirm on your chart before entering."
    )

# ── Main scan ─────────────────────────────────────────────────────────────────

def run_scan(force=False):
    session, nyt_dt = current_session()

    if not session and not force:
        print(f"[{nyt_dt.strftime('%H:%M')} NYT] Outside session windows — skipping.")
        return

    if force and not session:
        session = "TEST"

    print(f"[{nyt_dt.strftime('%H:%M')} NYT] Scanning — {session} session")

    for sym in PAIRS:
        pair_display = sym.replace("/", "")

        if not force and already_alerted(pair_display, session, nyt_dt):
            print(f"  {pair_display}: already alerted this session — skipping")
            continue

        try:
            candles = fetch_candles(sym)
            setup   = detect_setup(candles)

            if setup:
                print(f"  {pair_display}: SETUP — {setup['direction']}")
                result = send_telegram(format_alert(pair_display, session, setup, nyt_dt))
                if result.get("ok"):
                    print(f"  {pair_display}: Alert sent ✅")
                    if not force:
                        mark_alerted(pair_display, session, nyt_dt)
                else:
                    print(f"  {pair_display}: Telegram error: {result}")
            else:
                print(f"  {pair_display}: no setup")

        except Exception as e:
            print(f"  {pair_display}: ERROR — {e}")

        time.sleep(9)

    print("Scan complete.")

if __name__ == "__main__":
    force = "--force" in sys.argv or "--test" in sys.argv
    if force:
        print("*** FORCE/TEST MODE ***\n")
    run_scan(force=force)
