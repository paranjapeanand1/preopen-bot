"""NSE pre-open top-5 card -> Telegram.

Runs on GitHub Actions each weekday. Waits until 9:10:20 IST (after the
pre-open order collection closes), fetches F&O pre-open data from NSE,
builds the top-5 PNG card and sends it to Telegram. If anything fails it
sends a Telegram alert so you can fall back to a manual CSV upload.

Local test without NSE or Telegram:
    python preopen_bot.py --csv MW-Pre-Open-Market-FO-19-Oct-2026.csv --no-send
"""
import argparse, io, math, os, re, sys, time, traceback
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


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="Use a downloaded CSV instead of fetching from NSE")
    ap.add_argument("--now", action="store_true", help="Fetch immediately, don't wait for 9:10")
    ap.add_argument("--no-send", action="store_true", help="Don't send to Telegram")
    a = ap.parse_args()
    global TOKEN
    if a.no_send:
        TOKEN = ""

    today = datetime.now(IST).date()
    date_label = today.strftime("%a %d %b %Y")
    note = ""

    if a.csv:
        df = pd.read_csv(a.csv, encoding="utf-8-sig", dtype=str)
        df.columns = [c.strip() for c in df.columns]
    else:
        if today.isoformat() in HOLIDAYS:
            print("NSE holiday, nothing to do"); return
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


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        alert("Pre-open card crashed. Send the CSV to Claude manually. Error:\n" + traceback.format_exc()[-600:])
        sys.exit(1)
