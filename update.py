from datetime import date
from models import db, HistoricalPrice, Dividend, Holding
import yfinance as yf
from sqlalchemy import and_

MIN_DATE = date(2025, 2, 18)
FAKE_TICKERS = {"SPLTN"}

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
    exchange_tickers = {
        "USD": "USDSEK=X",
        "EUR": "EURSEK=X",
        "NOK": "NOKSEK=X",
    }

    exchange_rates = {}
    for cur, ticker in exchange_tickers.items():
        hist = yf.Ticker(ticker).history(period="max")['Close']
        exchange_rates[cur] = hist
    return exchange_rates

def convert_to_sek(amount, currency, date_, exchange_rates):
    if currency == "SEK":
        return amount
    try:
        rate_series = exchange_rates[currency]
        rate = rate_series.get(date_)
        if rate is None:
            rate = rate_series[rate_series.index <= date_].iloc[-1]
        return amount * rate
    except Exception as e:
        return amount * 1.0 

def update_close_prices(ticker, start_date=None, include_dividends=False):
    if ticker in FAKE_TICKERS:
        return

    yf_ticker = yf.Ticker(ticker)
    hist = yf_ticker.history(start=MIN_DATE)
    currency = infer_currency(ticker)

    exchange_rates = get_exchange_history()

    for dt, row in hist.iterrows():
        date_only = dt.date()
        if start_date and date_only < start_date:
            continue
        if row['Close'] is None or row['Close'] == 0:
            continue

        price = convert_to_sek(row['Close'], currency, dt, exchange_rates)
        db.session.merge(HistoricalPrice(
            ticker=ticker,
            date=date_only,
            close=price
        ))

    if include_dividends:
        divs = hist['Dividends']
        for dt, amount in divs.items():
                date_only = dt.date()
                if date_only < MIN_DATE or not amount or amount == 0:
                    continue
                holding = (
                    db.session.query(Holding)
                    .filter(and_(
                        Holding.ticker == ticker,
                        Holding.date <= date_only
                    ))
                    .order_by(Holding.date.desc())
                    .first()
                )
            
                if not holding:
                    continue

                if holding.shares <= 0:
                    continue

                dividend_amount = convert_to_sek(amount, currency, dt, exchange_rates)
                db.session.merge(Dividend(
                    ticker=ticker,
                    date=date_only,
                    amount=dividend_amount
                ))

                #print(f"Dividend added: {ticker} on {date_only} = {dividend_amount:.2f} SEK (Held {holding.shares} shares)")

    db.session.commit()
