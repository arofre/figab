from datetime import date
from models import db, HistoricalPrice, Dividend, Holding
import yfinance as yf
from sqlalchemy import and_

MIN_DATE = date(2025, 2, 18)
FAKE_TICKERS = {"SPLTN"}

# Simple in-memory cache for exchange rates
EXCHANGE_CACHE = {}

def infer_currency(ticker):
    if ".ST" in ticker:
        return "SEK"
    elif ".PA" in ticker or ".AS" in ticker:
        return "EUR"
    elif ".OL" in ticker:
        return "NOK"
    else:
        return "USD"

def get_exchange_history():
    global EXCHANGE_CACHE
    if EXCHANGE_CACHE:
        return EXCHANGE_CACHE

    exchange_tickers = {"USD": "USDSEK=X", "EUR": "EURSEK=X", "NOK": "NOKSEK=X"}
    exchange_rates = {}
    for cur, ticker in exchange_tickers.items():
        try:
            hist = yf.Ticker(ticker).history(period="max")['Close']
            exchange_rates[cur] = hist
        except Exception:
            # If delisted or failed, mark as empty Series
            exchange_rates[cur] = hist if 'hist' in locals() else None
    EXCHANGE_CACHE = exchange_rates
    return exchange_rates

def convert_to_sek(amount, currency, date_, exchange_rates):
    if currency == "SEK" or currency not in exchange_rates or exchange_rates[currency] is None:
        return amount
    rate_series = exchange_rates[currency]
    if date_ in rate_series:
        rate = rate_series[date_]
    else:
        rate_series = rate_series[rate_series.index <= date_]
        rate = rate_series.iloc[-1] if not rate_series.empty else 1.0
    return amount * rate

def update_close_prices(ticker, start_date=None, include_dividends=False, batch_size=200):
    """Optimized: batch DB commits, skip fake/delisted tickers, cache exchange rates."""
    if ticker in FAKE_TICKERS or not ticker:
        return

    yf_ticker = yf.Ticker(ticker)
    try:
        hist = yf_ticker.history(start=MIN_DATE)
    except Exception:
        return  # skip ticker if network/data fails

    try:
        info = yf_ticker.get_info()
        long_name = info.get("longName") or info.get("shortName") or ticker
        sector = info.get("sector") or "Unknown"
    except Exception:
        long_name = ticker
        sector = "Unknown"

    is_index = ticker.startswith("^")
    currency = "SEK" if ticker == "^OMX" else "USD" if ticker == "^GSPC" else infer_currency(ticker)
    exchange_rates = {} if is_index else get_exchange_history()

    # Collect objects to commit in batches
    db_objects = []

    if not include_dividends:
        for dt, row in hist.iterrows():
            date_only = dt.date()
            if start_date and date_only < start_date:
                continue
            close_price = row.get('Close')
            if not close_price or close_price == 0:
                continue
            price = close_price if is_index else convert_to_sek(close_price, currency, dt, exchange_rates)
            db_objects.append(HistoricalPrice(ticker=ticker, date=date_only, close=price))
            if len(db_objects) >= batch_size:
                db.session.bulk_save_objects(db_objects)
                db.session.commit()
                db_objects = []

    if include_dividends:
        divs = hist['Dividends']
        for dt, amount in divs.items():
            date_only = dt.date()
            if date_only < MIN_DATE or not amount or amount == 0:
                continue
            # Get most recent holding
            holding = (
                db.session.query(Holding)
                .filter(and_(Holding.ticker == ticker, Holding.date <= date_only))
                .order_by(Holding.date.desc())
                .first()
            )
            if holding and holding.shares > 0:
                holding.longname = long_name
                holding.sector = sector
                db_objects.append(Dividend(ticker=ticker, date=date_only, amount=convert_to_sek(amount, currency, dt, exchange_rates)))
            if len(db_objects) >= batch_size:
                db.session.bulk_save_objects(db_objects)
                db.session.commit()
                db_objects = []

    if db_objects:
        db.session.bulk_save_objects(db_objects)
        db.session.commit()
