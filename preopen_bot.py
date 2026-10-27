"""NSE pre-open top-5 card -> Telegram.

Runs on GitHub Actions each weekday. Waits until 9:10:20 IST (after the
pre-open order collection closes), fetches F&O pre-open data from NSE,
builds the top-5 PNG card and sends it to Telegram. If anything fails it
sends a Telegram alert so you can fall back to a manual CSV upload.

Local test without NSE or Telegram:
    python preopen_bot.py --csv MW-Pre-Open-Market-FO-19-Oct-2026.csv --no-send
"""
import argparse, io, math, os, re, sys, time, traceback, zipfile, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from PIL import Image, ImageDraw, ImageFont

IST = ZoneInfo("Asia/Kolkata")
KEY = os.environ.get("PREOPEN_KEY", "FO")           # FO = F&O stocks, NIFTY = Nifty 50, ALL = all
UNIVERSE = {"FO": "F&O stocks", "NIFTY": "Nifty 50", "ALL": "All stocks"}.get(KEY, KEY)
FETCH_AT = os.environ.get("FETCH_AT", "09:10:20")    # IST
GIVE_UP_AT = os.environ.get("GIVE_UP_AT", "09:14:00")  # IST
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
HOLIDAYS = {d.strip() for d in os.environ.get("NSE_HOLIDAYS", "").split(",") if d.strip()}  # YYYY-MM-DD
DEBUG_DIR = "debug"

BASE = "https://www.nseindia.com"
PAGE = BASE + "/market-data/pre-open-market-cm-and-emerge-market"
API = BASE + "/api/market-data-pre-open?key=" + KEY
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": PAGE,
}


# ---------------------------------------------------------------- telegram
def tg(method, data=None, files=None):
    if not TOKEN or not CHAT:
        print("Telegram not configured; skipping", method)
        return
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}",
                      data={"chat_id": CHAT, **(data or {})}, files=files, timeout=30)
    print("Telegram", method, r.status_code, r.text[:200])


def alert(text):
    tg("sendMessage", {"text": text, "disable_web_page_preview": "true"})


# ---------------------------------------------------------------- timing
def wait_until(hhmmss):
    now = datetime.now(IST)
    h, m, s = map(int, hhmmss.split(":"))
    target = now.replace(hour=h, minute=m, second=s, microsecond=0)
    secs = (target - now).total_seconds()
    if secs > 0:
        print(f"Now {now:%H:%M:%S} IST, sleeping {int(secs)}s until {hhmmss}")
        time.sleep(secs)


def past(hhmmss):
    h, m, s = map(int, hhmmss.split(":"))
    now = datetime.now(IST)
    return now >= now.replace(hour=h, minute=m, second=s, microsecond=0)


# ---------------------------------------------------------------- NSE fetch
def nse_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    for url in (BASE, PAGE):
        try:
            s.get(url, timeout=15)
        except requests.RequestException as e:
            print("Warm-up failed", url, e)
    return s


def save_debug(name, content):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    with open(os.path.join(DEBUG_DIR, name), "wb") as f:
        f.write(content if isinstance(content, bytes) else content.encode())


def fetch_json(s):
    r = s.get(API, timeout=20)
    save_debug("preopen.json", r.content)
    r.raise_for_status()
    return r.json()


def fetch_csv(s):
    """Same file as the page's Download CSV button, when NSE serves it."""
    r = s.get(API + "&csv=true", timeout=20)
    save_debug("preopen.csv", r.content)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.content.decode("utf-8-sig")), dtype=str)
    df.columns = [c.strip() for c in df.columns]
    need = {"SYMBOL", "BEST BID QTY", "BEST BID PRICE", "BEST ASK PRICE", "BEST ASK QTY", "%CHANGE", "FINAL PRICE"}
    if not need.issubset(df.columns):
        raise ValueError("CSV missing columns: " + ", ".join(sorted(need - set(df.columns))))
    return df


def json_to_df(js):
    """Fallback when the CSV isn't available. Best bid/ask are taken from the
    pre-open order levels, which may differ slightly from the CSV."""
    rows = []
    for it in js.get("data", []):
        m = it.get("metadata", {}) or {}
        p = (it.get("detail", {}) or {}).get("preOpenMarket", {}) or {}
        lv = p.get("preopen", []) or []
        bids = [(float(l["price"]), l.get("buyQty", 0)) for l in lv if l.get("buyQty") and l.get("price")]
        asks = [(float(l["price"]), l.get("sellQty", 0)) for l in lv if l.get("sellQty") and l.get("price")]
        bb = max(bids) if bids else (None, 0)
        ba = min(asks) if asks else (None, 0)
        rows.append({
            "SYMBOL": m.get("symbol"),
            "PREV. CLOSE": m.get("previousClose", p.get("prevClose")),
            "BEST BID QTY": bb[1], "BEST BID PRICE": bb[0],
            "BEST ASK PRICE": ba[0], "BEST ASK QTY": ba[1],
            "%CHANGE": m.get("pChange", p.get("perChange")),
            "FINAL PRICE": p.get("finalPrice") or m.get("iep") or m.get("lastPrice"),
            "FINAL VOLUME": p.get("finalQuantity") or m.get("finalQuantity") or 0,
        })
    return pd.DataFrame(rows).astype(str)


def data_date(js):
    """Date of the pre-open data, from NSE's update time (None if not found)."""
    for it in js.get("data", [])[:5]:
        t = ((it.get("detail", {}) or {}).get("preOpenMarket", {}) or {}).get("lastUpdateTime")
        if t:
            try:
                return datetime.strptime(t.split()[0], "%d-%b-%Y").date()
            except ValueError:
                pass
    return None


# ---------------------------------------------------------------- analysis
def num(v):
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "nan", "None"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def analyse(df):
    rows = []
    for _, r in df.iterrows():
        fp = num(r["FINAL PRICE"])
        if math.isnan(fp):
            continue
        g = num(r["%CHANGE"]); g = 0.0 if math.isnan(g) else g
        bq = num(r["BEST BID QTY"]); bq = 0 if math.isnan(bq) else int(bq)
        aq = num(r["BEST ASK QTY"]); aq = 0 if math.isnan(aq) else int(aq)
        bid, ask = num(r["BEST BID PRICE"]), num(r["BEST ASK PRICE"])
        side = "BID" if abs(fp - bid) < 1e-3 else "ASK" if abs(fp - ask) < 1e-3 else "MID"
        if side == "MID":
            continue  # final price must equal best bid or best ask
        un = bq if side == "BID" else aq if side == "ASK" else 0
        ot = aq if side == "BID" else bq if side == "ASK" else 0
        ratio = (un + 1) / (ot + 1) if side != "MID" else 1
        sig = "Neutral"
        if ratio >= 3 and g > 0: sig = "Bullish" if side == "BID" else "Fade risk"
        if ratio >= 3 and g < 0: sig = "Bounce" if side == "BID" else "Bearish"
        s = (min(abs(g) / 3, 1) * 50 + min(math.log10(max(ratio, 1)) / 2, 1) * 35
             + min(math.log10(un * fp / 1e5 + 1) / 2.5, 1) * 15)
        if sig == "Neutral": s *= 0.4
        rows.append(dict(sym=str(r["SYMBOL"]), g=g, side=side, ratio=ratio, sig=sig, score=round(s),
                         bq=bq, aq=aq, fp=fp, trade=abs(g) >= 1.5))
    rows.sort(key=lambda x: (not x["trade"], -x["score"]))
    gaps = [num(v) for v in df["%CHANGE"]]
    adv = sum(1 for x in gaps if x > 0); dec = sum(1 for x in gaps if x < 0)
    breadth = dict(adv=adv, dec=dec, unc=len(df) - adv - dec, gap=sum(1 for x in rows if x["trade"]), n=len(df))
    return rows[:5], breadth


# ---------------------------------------------------------------- card
FD = "/usr/share/fonts/truetype/dejavu/"
def font(size, bold=False):
    return ImageFont.truetype(FD + ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), size)

BG, CARD, FG, MUT, LINE = "#F7F6F2", "#FFFFFF", "#1F1E1D", "#73726C", "#E3E1D8"
COL = {"Bullish": ("#2E7D32", "#E6F2E3", "▲"), "Bearish": ("#C62828", "#FBE7E7", "▼"),
       "Fade risk": ("#B26A00", "#FCF0DB", "⚠"), "Bounce": ("#1565C0", "#E3EEFA", "↺"),
       "Neutral": ("#73726C", "#EFEEE9", "–")}
PLAN = {"Bullish": "Buy only if it holds above final price after 9:15",
        "Fade risk": "Short only if it fails at final price after 9:15",
        "Bearish": "Short only if it stays below final price after 9:15",
        "Bounce": "Buy only if it reclaims final price after 9:15",
        "Neutral": "No pre-open edge — wait for price action"}


def inr(x):
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def ind(n):
    s = str(int(n))
    if len(s) <= 3: return s
    return re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", s[:-3]) + "," + s[-3:]


def mood(b):
    a, d = b["adv"], b["dec"]
    if a >= 2 * max(d, 1): return "Strongly positive"
    if a > 1.2 * d: return "Positive"
    if a * 1.2 >= d: return "Mixed"
    if a * 2 > d: return "Negative"
    return "Strongly negative"


def build_card(top, b, out, date_label, note=""):
    W, P, CH = 1080, 48, 330
    H = 330 + len(top) * (CH + 24) + 120 + (34 if note else 0)
    im = Image.new("RGB", (W, H), BG); dr = ImageDraw.Draw(im)
    rr = lambda box, fill, r=24, outline=None, w=0: dr.rounded_rectangle(box, r, fill=fill, outline=outline, width=w)
    dr.text((P, 44), "Pre-open top 5", font=font(52, True), fill=FG)
    dr.text((P, 112), f"{date_label}  ·  {UNIVERSE}  ·  {b['n']} stocks", font=font(28), fill=MUT)
    dr.text((P, 170), f"Breadth: {mood(b)}", font=font(30, True), fill=FG)
    x0, x1, y = P, W - P, 220
    tot = max(b["adv"] + b["dec"] + b["unc"], 1)
    xa = x0 + (x1 - x0) * b["adv"] / tot; xd = xa + (x1 - x0) * b["dec"] / tot
    rr((x0, y, x1, y + 18), "#D9D7CE", 9)
    if b["adv"]: rr((x0, y, max(xa, x0 + 18), y + 18), "#2E7D32", 9)
    if b["dec"]: dr.rectangle((xa, y, xd, y + 18), fill="#C62828")
    dr.text((P, 252), f"{b['adv']} up   {b['dec']} down   {b['unc']} flat   ·   {b['gap']} gapped 1.5%+", font=font(26), fill=MUT)
    y = 310
    for i, x in enumerate(top):
        c, bgc, ic = COL[x["sig"]]
        rr((P, y, W - P, y + CH), CARD, 28, outline=c if x["trade"] else LINE, w=3 if x["trade"] else 2)
        dr.rectangle((P + 2, y + 26, P + 12, y + CH - 26), fill=c)
        ix = P + 40
        dr.text((ix, y + 28), f"#{i+1}", font=font(26), fill=MUT)
        dr.text((ix + 56, y + 20), x["sym"], font=font(42, True), fill=FG)
        lab = f"{ic} {x['sig']}"; tw = dr.textlength(lab, font=font(30, True))
        rr((W - P - 80 - tw, y + 22, W - P - 40, y + 76), bgc, 27)
        dr.text((W - P - 60 - tw, y + 30), lab, font=font(30, True), fill=c)
        dr.text((ix, y + 92), f"{'+' if x['g'] > 0 else ''}{x['g']:.2f}%", font=font(64, True), fill=FG)
        dr.text((ix, y + 168), "Gap" + ("" if x["trade"] else "  ·  watchlist (<1.5%)"), font=font(24), fill=MUT)
        sx = W - P - 40; st = str(x["score"])
        dr.text((sx - dr.textlength(st, font=font(56, True)), y + 96), st, font=font(56, True), fill=c)
        dr.text((sx - dr.textlength("score", font=font(24)), y + 168), "score", font=font(24), fill=MUT)
        by = y + 214; bx0, bx1 = ix, W - P - 40
        t = max(x["bq"] + x["aq"], 1); bw = max(0.04, min(0.96, x["bq"] / t)); mid = bx0 + (bx1 - bx0) * bw
        rr((bx0, by, mid - 3, by + 16), "#2E7D32" if x["side"] != "ASK" else "#B9D7B5", 8)
        rr((mid + 3, by, bx1, by + 16), "#C62828" if x["side"] != "BID" else "#F0B9B9", 8)
        fb, fa = font(26, x["side"] == "BID"), font(26, x["side"] == "ASK")
        dr.text((bx0, by + 26), f"Buyers {ind(x['bq'])}", font=fb, fill=FG if x["side"] == "BID" else MUT)
        stx = f"Sellers {ind(x['aq'])}"
        dr.text((bx1 - dr.textlength(stx, font=fa), by + 26), stx, font=fa, fill=FG if x["side"] == "ASK" else MUT)
        dr.text((ix, y + CH - 52), f"Final {inr(x['fp'])}  ·  {PLAN[x['sig']]}", font=font(24), fill=FG)
        y += CH + 24
    dr.text((P, y + 10), "Solid border = gap 1.5%+  ·  Score = gap + bid/ask imbalance + leftover value", font=font(22), fill=MUT)
    dr.text((P, y + 44), "Educational only, not investment advice. Confirm with price action after 9:15.", font=font(22), fill=MUT)
    if note:
        dr.text((P, y + 78), note, font=font(22), fill="#B26A00")
    im.save(out)


def caption(top, b, date_label):
    lines = [f"Pre-open top 5 · {date_label} · {mood(b)} breadth ({b['adv']}↑ {b['dec']}↓)"]
    for i, x in enumerate(top):
        lines.append(f"{i+1}. {x['sym']} {COL[x['sig']][2]} {x['sig']} · gap {x['g']:+.2f}% · score {x['score']}"
                     + ("" if x["trade"] else " · watchlist"))
    return "\n".join(lines)



# ================================================================ MOMENTUM (separate list)
# Direction-free: which stocks are likely to MOVE in the first 15 minutes.
# Score = gap (40) + relative pre-open volume (30) + leftover order value (30).
# Relative volume = pre-open volume / 20-day average daily volume (from NSE bhavcopy).
ARCH_NEW = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
ARCH_OLD = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"
AVG_DAYS = 20


def _bhav_day(s, d):
    for url in (ARCH_NEW.format(d=d), ARCH_OLD.format(d=d)):
        try:
            r = s.get(url, timeout=20)
            if r.status_code != 200 or len(r.content) < 1000:
                continue
            if url.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    raw = z.read(z.namelist()[0]).decode("utf-8-sig")
            else:
                raw = r.content.decode("utf-8-sig")
            t = pd.read_csv(io.StringIO(raw), dtype=str)
            t.columns = [c.strip() for c in t.columns]
            if "TckrSymb" in t.columns:
                t = t[t["SctySrs"].str.strip() == "EQ"]; sym, vol = t["TckrSymb"], t["TtlTradgVol"]
            elif "SYMBOL" in t.columns:
                t = t[t["SERIES"].str.strip() == "EQ"]; sym, vol = t["SYMBOL"], t["TTL_TRD_QNTY"]
            else:
                continue
            v = pd.to_numeric(vol.str.replace(",", "").str.strip(), errors="coerce")
            return dict(zip(sym.str.strip(), v))
        except Exception as e:
            print("Bhavcopy", d, url[-30:], "failed:", e)
    return None


def avg_volumes(s, today):
    """20-day average daily volume per symbol, from NSE's daily bhavcopy files."""
    vols, got, d = {}, 0, today - timedelta(days=1)
    for _ in range(45):
        if got >= AVG_DAYS:
            break
        if d.weekday() < 5:
            day = _bhav_day(s, d)
            if day:
                for k, v in day.items():
                    if v == v:
                        vols.setdefault(k, []).append(v)
                got += 1
        d -= timedelta(days=1)
    print(f"Average volume built from {got} bhavcopy days")
    if got < 5:
        return {}
    need = max(3, got // 2)
    return {k: sum(v) / len(v) for k, v in vols.items() if len(v) >= need}


# ---- Primary source: Upstox public historical candles (no login or token needed)
UPSTOX_MASTER = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
UPSTOX_CANDLE = "https://api.upstox.com/v3/historical-candle/{key}/days/1/{to}/{frm}"


def avg_volumes_upstox(symbols, today):
    """20-day average daily volume for each symbol from Upstox daily candles.
    Uses only sessions before today. Returns {symbol: avg volume}."""
    import gzip
    from urllib.parse import quote
    from concurrent.futures import ThreadPoolExecutor
    r = requests.get(UPSTOX_MASTER, timeout=60)
    r.raise_for_status()
    inst = json.loads(gzip.decompress(r.content))
    keys = {i.get("trading_symbol", "").upper(): i.get("instrument_key")
            for i in inst if i.get("segment") == "NSE_EQ" and i.get("instrument_type") == "EQ"}
    if not symbols:   # before pre-open: use today's F&O stock list from the same file
        by_key = {v: k for k, v in keys.items()}
        symbols = sorted({by_key.get(i.get("underlying_key")) for i in inst
                          if i.get("segment") == "NSE_FO" and i.get("instrument_type") == "FUT"
                          and str(i.get("underlying_key", "")).startswith("NSE_EQ|")} - {None})
        print(f"Upstox: {len(symbols)} F&O stocks in instrument master")
    to = (today - timedelta(days=1)).isoformat()
    frm = (today - timedelta(days=45)).isoformat()

    def one(sym):
        k = keys.get(sym.upper())
        if not k:
            return sym, None
        url = UPSTOX_CANDLE.format(key=quote(k, safe=""), to=to, frm=frm)
        for attempt in range(3):
            try:
                resp = requests.get(url, headers={"Accept": "application/json"}, timeout=20)
                if resp.status_code == 429:
                    time.sleep(2 + attempt * 2); continue
                resp.raise_for_status()
                c = resp.json().get("data", {}).get("candles", [])
                c = sorted(c, key=lambda x: x[0], reverse=True)          # newest first
                c = [x for x in c if str(x[0])[:10] < today.isoformat()][:AVG_DAYS]
                vols = [float(x[5]) for x in c if x[5] is not None]
                return sym, (sum(vols) / len(vols) if len(vols) >= 10 else None)
            except Exception as e:
                last = e
                time.sleep(1)
        print("Upstox candles failed for", sym, last)
        return sym, None

    with ThreadPoolExecutor(max_workers=5) as ex:
        res = dict(ex.map(one, symbols))
    out = {k: v for k, v in res.items() if v}
    print(f"Upstox: 20-day average volume for {len(out)} of {len(symbols)} symbols")
    return out


def get_avg_volumes(symbols, today):
    """Upstox first; NSE bhavcopy only if Upstox covers under 80% of stocks."""
    src = ""
    avgv = {}
    try:
        avgv = avg_volumes_upstox(symbols, today); src = "Upstox"
    except Exception as e:
        print("Upstox average volume failed:", e)
    if len(avgv) < 150 if not symbols else len(avgv) < 0.8 * len(symbols):
        try:
            nse = avg_volumes(nse_session(), today)
            if len(nse) > len(avgv):
                avgv, src = nse, "NSE bhavcopy"
        except Exception as e:
            print("NSE bhavcopy fallback failed:", e)
    return avgv, src


# Blast watch flag: unusual pre-open volume + real gap. Flagged stocks always get a slot on the card.
BLAST_RV, BLAST_GAP = 4.0, 1.5      # pre-open vol >= 4% of 20-day avg AND |gap| >= 1.5%

def momentum(df, avgv):
    rows = []
    for _, r in df.iterrows():
        fp = num(r["FINAL PRICE"])
        if math.isnan(fp):
            continue
        sym = str(r["SYMBOL"]).strip()
        g = num(r["%CHANGE"]); g = 0.0 if math.isnan(g) else g
        fv = num(r.get("FINAL VOLUME", 0)); fv = 0 if math.isnan(fv) else fv
        bq = num(r["BEST BID QTY"]); bq = 0 if math.isnan(bq) else bq
        aq = num(r["BEST ASK QTY"]); aq = 0 if math.isnan(aq) else aq
        left_q = max(bq, aq); left_side = "buyers" if bq >= aq else "sellers"
        left_l = left_q * fp / 1e5                         # ₹ lakh
        gp = min(abs(g) / 3, 1) * 40
        av = avgv.get(sym)
        if av:
            rv = fv / av * 100                              # % of a normal day
            vp = min(math.log10(1 + rv / 0.1) / math.log10(51), 1) * 30   # full at 5%
        else:
            rv = None                                       # no history: fall back to turnover
            vp = min(math.log10(fv * fp / 1e7 + 1) / math.log10(51), 1) * 30
        lp = min(math.log10(left_l + 1) / 2.5, 1) * 30
        rows.append(dict(sym=sym, g=g, fp=fp, rv=rv, left_l=left_l, left_side=left_side,
                         gp=gp, vp=vp, lp=lp, score=round(gp + vp + lp),
                         blast=rv is not None and rv >= BLAST_RV and abs(g) >= BLAST_GAP))
    rows.sort(key=lambda x: -x["score"])
    top = rows[:5]
    for x in rows[5:]:                                  # guarantee a slot for flagged stocks
        if x["blast"]:
            free = [i for i, t in enumerate(top) if not t["blast"]]
            if not free:
                break
            top[free[-1]] = x
    top.sort(key=lambda x: -x["score"])
    return top


def build_momentum_card(top, out, date_label, rv_ok, src=""):
    W, P, CH = 1080, 48, 250
    ACC = "#5B4FCF"
    BL, BLB = "#D84315", "#FDE7DF"
    H = 230 + len(top) * (CH + 22) + 164
    im = Image.new("RGB", (W, H), BG); dr = ImageDraw.Draw(im)
    rr = lambda box, fill, r=24, outline=None, w=0: dr.rounded_rectangle(box, r, fill=fill, outline=outline, width=w)
    dr.text((P, 44), "Momentum top 5", font=font(52, True), fill=FG)
    dr.text((P, 112), f"{date_label}  ·  {UNIVERSE}  ·  likely movers, no direction", font=font(28), fill=MUT)
    dr.text((P, 160), "Trade the 9:15 breakout either way  ·  exit by 9:30", font=font(28, True), fill=ACC)
    y = 222
    for i, x in enumerate(top):
        bl = x.get("blast")
        rr((P, y, W - P, y + CH), CARD, 28, outline=BL if bl else LINE, w=4 if bl else 2)
        dr.rectangle((P + 2, y + 26, P + 12, y + CH - 26), fill=BL if bl else ACC)
        ix = P + 40
        dr.text((ix, y + 26), f"#{i+1}", font=font(26), fill=MUT)
        dr.text((ix + 56, y + 18), x["sym"], font=font(42, True), fill=FG)
        if bl:
            bx = ix + 56 + dr.textlength(x["sym"], font=font(42, True)) + 20
            lab = "BLAST WATCH"; tw = dr.textlength(lab, font=font(24, True))
            rr((bx, y + 24, bx + tw + 36, y + 66), BLB, 21)
            dr.text((bx + 18, y + 31), lab, font=font(24, True), fill=BL)
        st = str(x["score"]); sx = W - P - 40
        dr.text((sx - dr.textlength(st, font=font(56, True)), y + 16), st, font=font(56, True), fill=ACC)
        dr.text((sx - dr.textlength("score", font=font(24)), y + 86), "score", font=font(24), fill=MUT)
        gc = "#2E7D32" if x["g"] > 0 else "#C62828" if x["g"] < 0 else FG
        dr.text((ix, y + 84), f"{'+' if x['g'] > 0 else ''}{x['g']:.2f}%", font=font(48, True), fill=gc)
        dr.text((ix + 250, y + 98), "gap", font=font(24), fill=MUT)
        # three mini bars
        by = y + 160; cw = (W - 2 * P - 80 - 40) / 3
        parts = [("Gap", x["gp"], 40, f"{abs(x['g']):.2f}%"),
                 ("Pre-open vol", x["vp"], 30, (f"{x['rv']:.1f}% of normal day" if x["rv"] is not None else "turnover (no history)")),
                 ("Leftover", x["lp"], 30, f"₹{x['left_l']:,.0f} L {x['left_side']}")]
        for j, (lab, pts, mx, txt) in enumerate(parts):
            bx = ix + j * (cw + 20)
            dr.text((bx, by - 4), lab, font=font(22), fill=MUT)
            rr((bx, by + 26, bx + cw, by + 38), "#E6E3F7", 6)
            rr((bx, by + 26, bx + max(12, cw * pts / mx), by + 38), ACC, 6)
            dr.text((bx, by + 46), txt, font=font(21), fill=FG)
        y += CH + 22
    dr.text((P, y + 10), "Score = gap (40) + pre-open volume vs 20-day average (30) + leftover order value (30)", font=font(22), fill=MUT)
    dr.text((P, y + 44), f"Blast watch = pre-open volume ≥{BLAST_RV:g}% of normal day + gap ≥{BLAST_GAP:g}% (always shown)", font=font(22, True), fill="#D84315")
    dr.text((P, y + 78), "Test list, not a direction call. Educational only, not investment advice.", font=font(22), fill=MUT)
    y += 34
    if not rv_ok:
        dr.text((P, y + 78), "20-day volume history unavailable today; used pre-open turnover instead.", font=font(22), fill="#B26A00")
    elif src:
        dr.text((P, y + 78), f"20-day average volume source: {src}", font=font(22), fill=MUT)
    im.save(out)


def momentum_caption(top, date_label):
    lines = [f"Momentum top 5 · {date_label} · trade the breakout either way, exit by 9:30"]
    for i, x in enumerate(top):
        rv = f"{x['rv']:.1f}% of normal vol" if x["rv"] is not None else "turnover"
        lines.append(f"{i+1}. {x['sym']} · gap {x['g']:+.2f}% · {rv} · score {x['score']}"
                     + (" · ⚡ BLAST WATCH" if x.get("blast") else ""))
    return "\n".join(lines)


# ================================================================ WATCHLIST (3rd card)
def burst_icon(size):
    """Explosion burst icon (red outer, orange middle, yellow core), anti-aliased."""
    S = size * 4; c = S / 2
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    peaks = [1.0, 0.80, 0.94, 0.74, 1.0, 0.84, 0.90, 0.76, 0.97, 0.82, 0.92]
    n = len(peaks)
    def ring(scale, valley, rot=0.0):
        pts = []
        for k in range(n):
            a = 2 * math.pi * k / n + rot
            r = peaks[k] * scale * c
            pts.append((c + r * math.sin(a), c - r * math.cos(a)))
            a2 = a + math.pi / n
            pts.append((c + valley * scale * c * math.sin(a2), c - valley * scale * c * math.cos(a2)))
        return pts
    d.polygon(ring(0.98, 0.50), fill="#D32F2F")
    d.polygon(ring(0.68, 0.48, 0.12), fill="#F57C00")
    d.polygon(ring(0.36, 0.55, 0.25), fill="#FFC107")
    return im.resize((size, size), Image.LANCZOS)


def build_watchlist_card(pre_syms, mom_syms, out, date_label, blast_syms=()):
    """Plain list of every stock on either card; ✓ per card, ★ when on both,
    burst icon next to the name for blast-watch stocks."""
    common = [x for x in pre_syms if x in mom_syms]
    rows = common + [x for x in pre_syms if x not in common] + [x for x in mom_syms if x not in pre_syms]
    W, P, RH = 1080, 48, 92
    H = 300 + len(rows) * RH + 110 + (44 if blast_syms else 0)
    im = Image.new("RGB", (W, H), BG); dr = ImageDraw.Draw(im)
    bicon = burst_icon(52)
    dr.text((P, 44), "Watchlist", font=font(52, True), fill=FG)
    dr.text((P, 112), date_label, font=font(30), fill=MUT)
    cols = [("Stock", P + 24), ("Pre-open", 520), ("Momentum", 720), ("Focus", 930)]
    y = 190
    dr.rounded_rectangle((P, y, W - P, y + 70), 18, fill=FG)
    for name, x in cols:
        f = font(28, True)
        tx = x if name == "Stock" else x - dr.textlength(name, font=f) / 2
        dr.text((tx, y + 18), name, font=f, fill="#FFFFFF")
    y += 82
    for i, sym in enumerate(rows):
        star = sym in common
        fill = "#FFF6DA" if star else (CARD if i % 2 == 0 else "#F1EFE8")
        dr.rounded_rectangle((P, y, W - P, y + RH - 10), 16, fill=fill,
                             outline="#E0B33A" if star else LINE, width=3 if star else 1)
        cy = y + (RH - 10) / 2
        dr.text((P + 24, cy - 22), sym, font=font(38, True), fill=FG)
        if sym in blast_syms:
            bx = int(P + 24 + dr.textlength(sym, font=font(38, True)) + 14)
            im.paste(bicon, (bx, int(cy - 26)), bicon)
        for flag, x in ((sym in pre_syms, 520), (sym in mom_syms, 720)):
            if flag:
                dr.line([(x - 16, cy + 2), (x - 4, cy + 14), (x + 18, cy - 14)], fill="#2E7D32", width=7)
        if star:
            r1, r2 = 24, 10
            pts = [(930 + (r1 if k % 2 == 0 else r2) * math.sin(math.pi * k / 5),
                    cy - (r1 if k % 2 == 0 else r2) * math.cos(math.pi * k / 5)) for k in range(10)]
            dr.polygon(pts, fill="#E0A800")
        y += RH
    dr.text((P, y + 14), f"★ on both cards: {len(common)}   ·   total stocks: {len(rows)}", font=font(26, True), fill=FG)
    if blast_syms:
        small = burst_icon(34)
        im.paste(small, (P, int(y + 50)), small)
        dr.text((P + 44, y + 54), f"Blast watch = pre-open volume ≥{BLAST_RV:g}% of normal day + gap ≥{BLAST_GAP:g}%",
                font=font(22, True), fill="#D84315")
        y += 44
    dr.text((P, y + 52), "Educational only, not investment advice.", font=font(22), fill=MUT)
    im.save(out)
    cap = f"Watchlist · {date_label}\n★ Both cards: " + (", ".join(common) if common else "none")
    if blast_syms:
        cap += "\n💥 Blast watch: " + ", ".join(blast_syms)
    return cap


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="Use a downloaded CSV instead of fetching from NSE")
    ap.add_argument("--now", action="store_true", help="Fetch immediately, don't wait for 9:10")
    ap.add_argument("--no-send", action="store_true", help="Don't send to Telegram")
    ap.add_argument("--date", help="Date label for --csv tests, e.g. 2026-10-19")
    ap.add_argument("--avgvol", help="JSON file {symbol: avg volume} for --csv tests")
    a = ap.parse_args()
    global TOKEN
    if a.no_send:
        TOKEN = ""

    today = datetime.strptime(a.date, "%Y-%m-%d").date() if a.date else datetime.now(IST).date()
    date_label = today.strftime("%a %d %b %Y")
    note = ""
    avgv, src = {}, ""

    if a.csv:
        if a.avgvol:
            avgv, src = json.load(open(a.avgvol)), "test file"
        df = pd.read_csv(a.csv, encoding="utf-8-sig", dtype=str)
        df.columns = [c.strip() for c in df.columns]
    else:
        if today.isoformat() in HOLIDAYS:
            print("NSE holiday, nothing to do"); return
        try:                                   # uses the 8:35–9:10 waiting time
            avgv, src = get_avg_volumes(None, today)
        except Exception as e:
            print("Average volume prefetch failed:", e)
        if not a.now:
            wait_until(FETCH_AT)
        df, last_err = None, None
        while df is None:
            try:
                s = nse_session()
                js = fetch_json(s)
                dd = data_date(js)
                if dd and dd != today:
                    print(f"Data is from {dd}, not today; market holiday?")
                    alert(f"Pre-open: NSE data is from {dd:%d %b}, not today. Market holiday? No card sent.")
                    return
                try:
                    df = fetch_csv(s)
                except Exception as e:
                    print("CSV unavailable, using JSON fallback:", e)
                    df = json_to_df(js)
                    note = "Built from NSE JSON fallback; bid/ask quantities may differ slightly from the CSV."
            except Exception as e:
                last_err = e
                print("Fetch attempt failed:", e)
                if past(GIVE_UP_AT):
                    break
                time.sleep(15)
        if df is None:
            alert("Pre-open card failed: couldn't fetch data from NSE "
                  f"({type(last_err).__name__}). Download the F&O pre-open CSV and send it to Claude manually: {PAGE}")
            sys.exit(1)

    top, b = analyse(df)
    if not top:
        alert("Pre-open card: no usable rows in today's data. Check the NSE page manually.")
        sys.exit(1)
    out = f"preopen-top5-{today:%d-%b-%Y}.png"
    build_card(top, b, out, date_label, note)
    cap = caption(top, b, date_label)
    print(cap)
    with open(out, "rb") as f:
        tg("sendPhoto", {"caption": cap}, files={"photo": f})

    # second card: momentum list (separate test; never affects the card above)
    try:
        if not a.csv and not avgv:   # prefetch failed: one late attempt
            avgv, src = get_avg_volumes([str(x).strip() for x in df["SYMBOL"]], today)
        mtop = momentum(df, avgv)
        mout = f"momentum-top5-{today:%d-%b-%Y}.png"
        build_momentum_card(mtop, mout, date_label, bool(avgv), src)
        mcap = momentum_caption(mtop, date_label)
        print(mcap)
        with open(mout, "rb") as f:
            tg("sendPhoto", {"caption": mcap}, files={"photo": f})
    except Exception:
        traceback.print_exc()
        alert("Momentum card failed today (the main card above is unaffected).")
        mtop = []

    # third card: watchlist (all stocks from both cards, common ones starred)
    try:
        wout = f"watchlist-{today:%d-%b-%Y}.png"
        wcap = build_watchlist_card([x["sym"] for x in top], [x["sym"] for x in mtop], wout, date_label,
                                    [x["sym"] for x in mtop if x.get("blast")])
        print(wcap)
        with open(wout, "rb") as f:
            tg("sendPhoto", {"caption": wcap}, files={"photo": f})
    except Exception:
        traceback.print_exc()
        alert("Watchlist card failed today (the other cards are unaffected).")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        alert("Pre-open card crashed. Send the CSV to Claude manually. Error:\n" + traceback.format_exc()[-600:])
        sys.exit(1)
