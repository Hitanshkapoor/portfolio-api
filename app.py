"""
Portfolio Intelligence — Price API
Flask + yfinance backend for NSE stock and Nifty 50 data
Deploy free on Render.com
"""
from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import datetime, os, logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# In-memory cache so repeated requests are instant within a session
_cache = {}

def fetch_closes(symbol: str, years: int = 6) -> dict | None:
    """
    Fetch daily adjusted closing prices using Ticker.history().
    Returns {YYYY-MM-DD: float} or None if unavailable.
    Uses Ticker().history() which is more reliable than yf.download()
    across yfinance versions — avoids MultiIndex column issues.
    """
    if symbol in _cache:
        log.info(f"Cache hit: {symbol}")
        return _cache[symbol]

    log.info(f"Fetching: {symbol}")
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(
            period=f"{years}y",
            interval="1d",
            auto_adjust=True,
            actions=False          # skip dividends/splits columns
        )

        if df is None or df.empty:
            log.warning(f"Empty result for {symbol}")
            return None

        # Ticker.history() always returns simple column names: Open, High, Low, Close, Volume
        if "Close" not in df.columns:
            log.warning(f"No Close column for {symbol}. Columns: {list(df.columns)}")
            return None

        closes = {
            str(idx.date()): round(float(v), 2)
            for idx, v in df["Close"].items()
            if v == v and v > 0   # skip NaN
        }

        if len(closes) < 50:
            log.warning(f"Too few data points for {symbol}: {len(closes)}")
            return None

        log.info(f"  ✓ {symbol}: {len(closes)} days")
        _cache[symbol] = closes
        return closes

    except Exception as e:
        log.error(f"Error fetching {symbol}: {e}")
        return None


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def health():
    """Health check + UptimeRobot keep-alive endpoint."""
    return jsonify({"status": "ok", "service": "portfolio-intelligence-api"})


@app.route("/api/stock/<symbol>")
def stock(symbol):
    """
    Daily closes for one NSE stock.
    Pass symbol WITHOUT suffix, e.g. /api/stock/RELIANCE
    """
    sym = symbol.strip().upper()
    px  = fetch_closes(sym + ".NS")
    if not px:
        return jsonify({"error": f"No data for {sym}.NS"}), 404
    return jsonify(px)


@app.route("/api/nifty")
def nifty():
    """Daily closes for Nifty 50 index."""
    px = fetch_closes("^NSEI")
    if not px:
        return jsonify({"error": "Nifty 50 data unavailable"}), 404
    return jsonify(px)


@app.route("/api/batch")
def batch():
    """
    Fetch multiple stocks + optionally Nifty in one request.
    GET /api/batch?symbols=RELIANCE,TCS,HDFCBANK&nifty=true
    Returns { "RELIANCE": {date: price}, ..., "_nifty": {date: price} }
    """
    raw      = request.args.get("symbols", "")
    want_ni  = request.args.get("nifty", "false").lower() == "true"
    symbols  = [s.strip().upper() for s in raw.split(",") if s.strip()][:30]

    if not symbols and not want_ni:
        return jsonify({"error": "Provide ?symbols= or ?nifty=true"}), 400

    result = {}

    for sym in symbols:
        px = fetch_closes(sym + ".NS")
        if px:
            result[sym] = px
        else:
            log.warning(f"Batch: no data for {sym}.NS")

    if want_ni:
        px = fetch_closes("^NSEI")
        if px:
            result["_nifty"] = px

    return jsonify(result)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
