"""
Portfolio Intelligence — Price API
Primary source: Twelve Data (free API key, cloud-friendly, NSE India supported)
Fallback: NSE India direct API, then yfinance
Deploy on Render.com — set TWELVEDATA_KEY as environment variable
"""
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, datetime, os, logging, time

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

_cache      = {}
TD_KEY      = os.environ.get("TWELVEDATA_KEY", "")   # set in Render env vars
TD_BASE     = "https://api.twelvedata.com"
NSE_BASE    = "https://www.nseindia.com/api"
MAX_SYMBOLS = 25   # Twelve Data free batch limit


# ── Twelve Data ──────────────────────────────────────────────────────────────

def td_fetch(symbols: list[str], want_nifty: bool = False) -> dict:
    """
    Batch-fetch up to 25 NSE stocks + optionally Nifty in ONE API call.
    Returns { SYMBOL: {date: price}, ..., _nifty: {date: price} }
    Consumes 1 API credit per symbol (free tier: 800/day).
    """
    if not TD_KEY:
        return {}

    results = {}
    end   = datetime.date.today()
    start = end.replace(year=end.year - 6)

    def call_td(syms_str: str, exchange: str = "NSE") -> dict | None:
        url = (
            f"{TD_BASE}/time_series"
            f"?symbol={syms_str}"
            f"&exchange={exchange}"
            f"&interval=1day"
            f"&start_date={start}"
            f"&end_date={end}"
            f"&outputsize=2000"
            f"&dp=2"
            f"&apikey={TD_KEY}"
        )
        try:
            r = requests.get(url, timeout=20)
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as e:
            log.error(f"Twelve Data request failed: {e}")
            return None

    # Stocks batch (Twelve Data accepts comma-separated symbols)
    if symbols:
        chunk_size = 8   # keep each batch small to stay within response limits
        for i in range(0, len(symbols), chunk_size):
            chunk  = symbols[i:i + chunk_size]
            sym_q  = ",".join(chunk)
            data   = call_td(sym_q)
            if not data:
                continue
            # Single symbol response: {meta:{}, values:[]} 
            # Multi-symbol response: {RELIANCE: {meta:{}, values:[]}, ...}
            if len(chunk) == 1:
                sym    = chunk[0]
                values = data.get("values", [])
                if values:
                    results[sym] = _parse_td_values(values)
            else:
                for sym in chunk:
                    sym_data = data.get(sym, {})
                    values   = sym_data.get("values", []) if isinstance(sym_data, dict) else []
                    if values:
                        results[sym] = _parse_td_values(values)
            time.sleep(0.2)   # be polite to the API

    # Nifty 50 — separate call with different symbol
    if want_nifty:
        nifty_data = call_td("NIFTY", "NSE")
        if nifty_data:
            values = nifty_data.get("values", [])
            if values:
                results["_nifty"] = _parse_td_values(values)

    return results


def _parse_td_values(values: list) -> dict:
    """Convert Twelve Data values list to {YYYY-MM-DD: float}."""
    px = {}
    for row in values:
        try:
            dt = row["datetime"][:10]   # YYYY-MM-DD
            cl = float(row["close"])
            if cl > 0:
                px[dt] = round(cl, 2)
        except Exception:
            continue
    return px


# ── NSE India fallback ────────────────────────────────────────────────────────

_nse_s = None

def get_nse_session():
    global _nse_s
    if _nse_s:
        return _nse_s
    s = requests.Session()
    s.headers.update({
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":          "application/json,text/html,*/*;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://www.nseindia.com/",
    })
    try:
        s.get("https://www.nseindia.com/", timeout=10)
        time.sleep(0.5)
    except Exception:
        pass
    _nse_s = s
    return s


def fetch_nse_equity(symbol: str) -> dict | None:
    end   = datetime.date.today()
    start = end.replace(year=end.year - 6)
    fmt   = lambda d: d.strftime("%d-%m-%Y")
    url   = (
        f"{NSE_BASE}/historical/cm/equity"
        f"?symbol={symbol}&series=[%22EQ%22]"
        f"&from={fmt(start)}&to={fmt(end)}"
    )
    try:
        r = get_nse_session().get(url, timeout=15)
        if r.status_code != 200:
            return None
        rows = r.json().get("data", [])
        px = {}
        for row in rows:
            try:
                dt = datetime.datetime.strptime(
                    row["CH_TIMESTAMP"], "%d-%b-%Y").strftime("%Y-%m-%d")
                cl = float(row["CH_CLOSING_PRICE"])
                if cl > 0:
                    px[dt] = round(cl, 2)
            except Exception:
                continue
        return px if len(px) > 50 else None
    except Exception as e:
        log.warning(f"NSE equity fallback failed [{symbol}]: {e}")
        return None


def fetch_nse_nifty() -> dict | None:
    end   = datetime.date.today()
    start = end.replace(year=end.year - 6)
    fmt   = lambda d: d.strftime("%d-%m-%Y")
    url   = (
        f"{NSE_BASE}/historical/indicesHistory"
        f"?indexType=NIFTY%2050"
        f"&from={fmt(start)}&to={fmt(end)}"
    )
    try:
        r = get_nse_session().get(url, timeout=15)
        if r.status_code != 200:
            return None
        rows = r.json().get("data", {}).get("indexCloseOnlineRecords", [])
        px = {}
        for row in rows:
            try:
                dt = datetime.datetime.strptime(
                    row["EOD_TIMESTAMP"], "%d-%b-%Y").strftime("%Y-%m-%d")
                cl = float(row["EOD_CLOSE_INDEX_VAL"])
                if cl > 0:
                    px[dt] = round(cl, 2)
            except Exception:
                continue
        return px if len(px) > 50 else None
    except Exception as e:
        log.warning(f"NSE Nifty fallback failed: {e}")
        return None


# ── yfinance last resort ──────────────────────────────────────────────────────

def fetch_yf(symbol: str, is_index: bool = False) -> dict | None:
    try:
        import yfinance as yf
        s = requests.Session()
        s.headers["User-Agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
        )
        yf_sym = symbol if is_index else symbol + ".NS"
        t  = yf.Ticker(yf_sym, session=s)
        df = t.history(period="6y", interval="1d", auto_adjust=True, actions=False)
        if df is None or df.empty or "Close" not in df.columns:
            return None
        px = {str(i.date()): round(float(v), 2)
              for i, v in df["Close"].items() if v == v and v > 0}
        return px if len(px) > 50 else None
    except Exception as e:
        log.warning(f"yfinance last resort failed [{symbol}]: {e}")
        return None


# ── Unified get with cache ────────────────────────────────────────────────────

def get_stock(symbol: str) -> dict | None:
    if symbol in _cache:
        return _cache[symbol]

    # Twelve Data first (most reliable on cloud)
    if TD_KEY:
        batch = td_fetch([symbol])
        px    = batch.get(symbol)
        if px and len(px) > 50:
            _cache[symbol] = px
            log.info(f"  ✓ {symbol} via Twelve Data ({len(px)}d)")
            return px

    # NSE India fallback
    px = fetch_nse_equity(symbol)
    if px:
        _cache[symbol] = px
        log.info(f"  ✓ {symbol} via NSE India ({len(px)}d)")
        return px

    # yfinance last resort
    px = fetch_yf(symbol)
    if px:
        _cache[symbol] = px
        log.info(f"  ✓ {symbol} via yfinance ({len(px)}d)")
        return px

    log.error(f"  ✗ {symbol}: all sources failed")
    return None


def get_nifty() -> dict | None:
    key = "__nifty__"
    if key in _cache:
        return _cache[key]

    if TD_KEY:
        batch = td_fetch([], want_nifty=True)
        px    = batch.get("_nifty")
        if px and len(px) > 50:
            _cache[key] = px
            log.info(f"  ✓ Nifty via Twelve Data ({len(px)}d)")
            return px

    px = fetch_nse_nifty()
    if px:
        _cache[key] = px
        log.info(f"  ✓ Nifty via NSE India ({len(px)}d)")
        return px

    px = fetch_yf("^NSEI", is_index=True)
    if px:
        _cache[key] = px
        log.info(f"  ✓ Nifty via yfinance ({len(px)}d)")
        return px

    return None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def health():
    return jsonify({
        "status":  "ok",
        "service": "portfolio-intelligence-api",
        "td_key":  "configured" if TD_KEY else "missing — set TWELVEDATA_KEY env var"
    })


@app.route("/api/stock/<symbol>")
def stock(symbol):
    px = get_stock(symbol.strip().upper())
    if not px:
        return jsonify({"error": f"No data for {symbol.upper()}"}), 404
    return jsonify(px)


@app.route("/api/nifty")
def nifty():
    px = get_nifty()
    if not px:
        return jsonify({"error": "Nifty 50 unavailable"}), 404
    return jsonify(px)


@app.route("/api/batch")
def batch():
    """
    Single endpoint the frontend calls — fetches all stocks + Nifty at once.
    GET /api/batch?symbols=RELIANCE,TCS,HDFCBANK&nifty=true
    """
    raw      = request.args.get("symbols", "")
    want_ni  = request.args.get("nifty", "false").lower() == "true"
    symbols  = [s.strip().upper() for s in raw.split(",") if s.strip()][:30]

    if not symbols and not want_ni:
        return jsonify({"error": "Provide ?symbols= or ?nifty=true"}), 400

    result = {}

    # Try Twelve Data batch first (most efficient — all stocks in 2-3 API calls)
    if TD_KEY:
        uncached = [s for s in symbols if s not in _cache]
        if uncached or want_ni:
            td_res = td_fetch(uncached, want_nifty=want_ni)
            for sym, px in td_res.items():
                if sym == "_nifty":
                    _cache["__nifty__"] = px
                elif len(px) > 50:
                    _cache[sym] = px

    # Build result from cache + individual fallback for any misses
    for sym in symbols:
        px = get_stock(sym)   # returns from cache if TD already fetched it
        if px:
            result[sym] = px

    if want_ni:
        px = get_nifty()
        if px:
            result["_nifty"] = px

    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
