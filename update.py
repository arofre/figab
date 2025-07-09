from datetime import date
from models import db, HistoricalPrice, Dividend
import yfinance as yf

USD_TO_SEK = 10.5
EUR_TO_SEK = 11.5
MIN_DATE = date(2025, 2, 18)

FAKE_TICKERS = {"SPLTN"}

def infer_currency(ticker):
    if ".ST" in ticker:
        return "SEK"
    elif ".PA" in ticker:
        return "EUR"
    elif ".AS" in ticker:
        return "EUR"
    elif ".OL" in ticker:
        return "NOK"
    else:
        return "USD"

def convert_to_sek(amount, currency):
    if currency == "SEK":
        return amount
    elif currency == "USD":
        return amount * USD_TO_SEK
    elif currency == "EUR":
        return amount * EUR_TO_SEK
    else:
        return amount * 1.0  # fallback

def update_close_prices(ticker, start_date=None):
    if ticker in FAKE_TICKERS:
        print(f"⚠️ Skipping dividend fetching for fake ticker: {ticker}")
        return

    yf_ticker = yf.Ticker(ticker)
    hist = yf_ticker.history(period="max")

    currency = infer_currency(ticker)

    for dt, row in hist.iterrows():
        if start_date and dt.date() < start_date:
            continue
        if row['Close'] is None or row['Close'] == 0:
            continue

        price = convert_to_sek(row['Close'], currency)
        db.session.merge(HistoricalPrice(
            ticker=ticker,
            date=dt.date(),
            close=price
        ))

    divs = hist['Dividends']
    for dt, amount in divs.items():
        if dt.date() < MIN_DATE:
            continue
        if amount and amount > 0:
            db.session.merge(Dividend(
                ticker=ticker,
                date=dt.date(),
                amount=convert_to_sek(amount, currency)
            ))

    db.session.commit()
