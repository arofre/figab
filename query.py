from models import db, HistoricalPrice
import pandas as pd

def get_previous_close_price(ticker, target_date):
    price = db.session.query(HistoricalPrice)\
        .filter(HistoricalPrice.ticker == ticker.upper(), HistoricalPrice.date < target_date)\
        .order_by(HistoricalPrice.date.desc())\
        .first()

    return price.close if price else None

def get_all_prices(ticker):
    prices = HistoricalPrice.query.filter_by(ticker=ticker.upper())\
        .order_by(HistoricalPrice.date).all()
    
    return [(p.date, p.close) for p in prices]

def get_moving_average(ticker, window=5):
    prices = get_all_prices(ticker)
    df = pd.DataFrame(prices, columns=["date", "close"])
    df["moving_avg"] = df["close"].rolling(window=window).mean()
    return df
