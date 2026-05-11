"""
Portfolio Intelligence — Price API
Uses NSE India directly (no Yahoo Finance dependency)
Falls back to yfinance with fixed session headers if NSE unavailable
Deploy free on Render.com
"""
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, datetime, os, logging, time, json

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app  = Flask(__name__)
CORS(app)

_cache = {}   # in-memory cache — survives within one Render instance session

# ── NSE India session (mimics a real browser) ────────────────────────────────
def nse_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://www.nseindia.com/",
    })
    # Warm up — get cookies by hitting the NSE homepage first
    try:
        s.get("https://www.nseindia.com/", timeout=10)
        time.sleep(0.5)
    except Exception:
        pass
    return s

_nse_s = nse_session()   # one shared session; re-created if it breaks

def reset_nse_session():
    global _nse_s
    _nse_s = nse_session()

# ── Source 1: NSE India historical equity API ────────────────────────────────
def fetch_nse_equity(symbol: str, years: int = 6) -> dict | None:
    """Fetch daily OHLC from NSE India for an equity symbol (e.g. RELIANCE)."""
    end   = datetime.date.today()
    start = end.replace(year=end.year - years)
    fmt   = lambda d: d.strftime("%d-%m-%Y")
    url   = (
        "https://www.nseindia.com/api/historical/cm/equity"
        f"?symbol={symbol}&series=[%22EQ%22]"
        f"&from={fmt(start)}&to={fmt(end)}"
    )
    try:
        r = _nse_s.get(url, timeout=15)
        if r.status_code != 200:
            reset_nse_session()
            return None
        data = r.json().get("data", [])
        if not data:
            return None
        px = {}
        for row in data:
            # row keys: CH_TIMESTAMP (DD-MMM-YYYY), CH_CLOSING_PRICE
            try:
                dt  = datetime.datetime.strptime(row["CH_TIMESTAMP"], "%d-%b-%Y").strftime("%Y-%m-%d")
                cl  = float(row["CH_CLOSING_PRICE"])
                if cl > 0:
                    px[dt] = round(cl, 2)
            except Exception:
                continue
        log.info(f"NSE equity {symbol}: {len(px)} days")
        return px if len(px) > 50 else None
    except Exception as e:
        log.error(f"NSE equity error [{symbol}]: {e}")
        reset_nse_session()
        return None

# ── Source 2: NSE India index historical API ─────────────────────────────────
def fetch_nse_index(index_name: str = "NIFTY 50", years: int = 6) -> dict | None:
    """Fetch Nifty 50 (or other index) history from NSE India."""
    end   = datetime.date.today()
    start = end.replace(year=end.year - years)
    fmt   = lambda d: d.strftime("%d-%m-%Y")
    url   = (
        "https://www.nseindia.com/api/historical/indicesHistory"
        f"?indexType={requests.utils.quote(index_name)}"
        f"&from={fmt(start)}&to={fmt(end)}"
    )
    try:
        r = _nse_s.get(url, timeout=15)
        if r.status_code != 200:
            reset_nse_session()
            return None
        data = r.json().get("data", {}).get("indexCloseOnlineRecords", [])
        if not data:
            return None
        px = {}
        for row in data:
            # row keys: EOD_TIMESTAMP (DD-MMM-YYYY), EOD_CLOSE_INDEX_VAL
            try:
                dt = datetime.datetime.strptime(row["EOD_TIMESTAMP"], "%d-%b-%Y").strftime("%Y-%m-%d")
                cl = float(row["EOD_CLOSE_INDEX_VAL"])
                if cl > 0:
                    px[dt] = round(cl, 2)
            except Exception:
                continue
        log.info(f"NSE index {index_name}: {len(px)} days")
        return px if len(px) > 50 else None
    except Exception as e:
        log.error(f"NSE index error [{index_name}]: {e}")
        reset_nse_session()
        return None

# ── Source 3: yfinance fallback with proper session headers ──────────────────
def fetch_yfinance(symbol: str, years: int = 6) -> dict | None:
    """Last-resort fallback using yfinance with browser-like headers."""
    try:
        import yfinance as yf
        s = requests.Session()
        s.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        ticker = yf.Ticker(symbol, session=s)
        df     = ticker.history(period=f"{years}y", interval="1d",
                                auto_adjust=True, actions=False)
        if df is None or df.empty or "Close" not in df.columns:
            return None
        px = {str(i.date()): round(float(v), 2)
              for i, v in df["Close"].items() if v == v and v > 0}
        log.info(f"yfinance {symbol}: {len(px)} days")
        return px if len(px) > 50 else None
    except Exception as e:
        log.error(f"yfinance error [{symbol}]: {e}")
        return None

# ── Unified fetch with caching + fallback chain ───────────────────────────────
def get_closes(symbol: str, is_index: bool = False) -> dict | None:
    if symbol in _cache:
        return _cache[symbol]

    px = None
    if is_index:
        px = fetch_nse_index("NIFTY 50")                 # NSE index API
        if not px: px = fetch_yfinance("^NSEI")           # yfinance fallback
    else:
        nse_sym = symbol.replace(".NS", "").upper()
        px = fetch_nse_equity(nse_sym)                    # NSE equity API
        if not px: px = fetch_yfinance(nse_sym + ".NS")   # yfinance fallback

    if px:
        _cache[symbol] = px
    return px

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status": "ok", "service": "portfolio-intelligence-api"})

@app.route("/api/stock/<symbol>")
def stock(symbol):
    sym = symbol.strip().upper()
    px  = get_closes(sym)
    if not px:
        return jsonify({"error": f"No data for {sym}"}), 404
    return jsonify(px)

@app.route("/api/nifty")
def nifty():
    px = get_closes("^NSEI", is_index=True)
    if not px:
        return jsonify({"error": "Nifty 50 unavailable"}), 404
    return jsonify(px)

@app.route("/api/batch")
def batch():
    raw     = request.args.get("symbols", "")
    want_ni = request.args.get("nifty", "false").lower() == "true"
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()][:30]

    result = {}
    for sym in symbols:
        px = get_closes(sym)
        if px: result[sym] = px

    if want_ni:
        px = get_closes("^NSEI", is_index=True)
        if px: result["_nifty"] = px

    return jsonify(result)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
