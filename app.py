"""
Portfolio Intelligence — Price API
Flask + yfinance backend for NSE stock and index data
Deploy free on Render.com | github.com/YOUR-USERNAME/portfolio-api
"""
from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import datetime, os, logging

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
CORS(app)  # allow all origins — needed for GitHub Pages → Render requests

# ── cache in memory so repeat requests are instant ─────────────────────────
_cache = {}

def get_closes(yf_symbol, years=6):
    if yf_symbol in _cache:
        return _cache[yf_symbol]
    end   = datetime.date.today()
    start = end.replace(year=end.year - years)
    try:
        df = yf.download(
            yf_symbol, start=str(start), end=str(end),
            interval="1d", auto_adjust=True, progress=False, threads=False
        )
        if df.empty:
            return None
        col = df["Close"].squeeze()
        result = {
            str(idx.date()): round(float(v), 2)
            for idx, v in col.items()
            if v == v and v > 0   # skip NaN
        }
        if len(result) > 50:
            _cache[yf_symbol] = result   # cache it
        return result if len(result) > 50 else None
    except Exception as e:
        logging.error(f"yfinance error [{yf_symbol}]: {e}")
        return None

# ── routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def health():
    """Health check — also used by UptimeRobot keep-alive ping."""
    return jsonify({"status": "ok", "service": "portfolio-intelligence-api"})

@app.route("/api/stock/<symbol>")
def stock(symbol):
    """Daily closes for one NSE stock. Pass symbol WITHOUT .NS, e.g. /api/stock/RELIANCE"""
    sym = symbol.upper().strip()
    px  = get_closes(sym + ".NS")
    if not px:
        return jsonify({"error": f"No data for {sym}.NS"}), 404
    return jsonify(px)

@app.route("/api/nifty")
def nifty():
    """Daily closes for Nifty 50 index."""
    px = get_closes("^NSEI")
    if not px:
        return jsonify({"error": "Nifty 50 data unavailable"}), 404
    return jsonify(px)

@app.route("/api/batch")
def batch():
    """
    Fetch multiple stocks in one round-trip.
    GET /api/batch?symbols=RELIANCE,TCS,HDFCBANK&nifty=true
    Returns { RELIANCE: {date: price, ...}, TCS: {...}, _nifty: {...} }
    """
    raw     = request.args.get("symbols", "")
    want_ni = request.args.get("nifty", "false").lower() == "true"
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()][:30]

    result = {}
    for sym in symbols:
        px = get_closes(sym + ".NS")
        if px:
            result[sym] = px

    if want_ni:
        px = get_closes("^NSEI")
        if px:
            result["_nifty"] = px

    return jsonify(result)

# ── run ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port)
