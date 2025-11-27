import csv
from datetime import date, timedelta
from collections import defaultdict
from models import db, Holding, Cash, Dividend, HistoricalPrice
from update import update_close_prices
import yfinance as yf

CSV_PATH = "transactions.csv"
DEFAULT_START_DATE = date(2025, 2, 17)
STARTING_CASH = 150_000

def load_transactions(csv_path):
    transactions = []
    price_points = defaultdict(dict)
    with open(csv_path, newline='', encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            ticker = row["Ticker"].strip().upper()
            tx_date = date.fromisoformat(row["Date"])
            tx_type = row["Type"]
            amount = int(row["Amount"])
            price_str = row.get("PRICE")
            if price_str:
                price_points[ticker][tx_type.lower()] = (tx_date, float(price_str))
            transactions.append((tx_date, ticker, tx_type, amount))
    return sorted(transactions, key=lambda x: x[0]), price_points

def interpolate_and_add_prices(price_points):
    for ticker, types in price_points.items():
        if "buy" in types and "sell" in types:
            buy_date, buy_price = types["buy"]
            sell_date, sell_price = types["sell"]
            delta_days = (sell_date - buy_date).days
            for i in range(delta_days + 1):
                current_date = buy_date + timedelta(days=i)
                if current_date.weekday() >= 5:
                    continue
                interpolated_price = buy_price + (sell_price - buy_price) * (i / delta_days)
                existing = HistoricalPrice.query.filter_by(ticker=ticker, date=current_date).first()
                if not existing:
                    db.session.add(HistoricalPrice(ticker=ticker, date=current_date, close=interpolated_price))
    db.session.commit()

def apply_dividends_to_cash():
    all_cash = {c.date: c for c in Cash.query.all()}
    all_holdings = defaultdict(dict)
    for h in Holding.query.all():
        all_holdings[h.date][h.ticker] = h.shares
    for div in Dividend.query.all():
        div_date = div.date
        ticker = div.ticker
        amount = div.amount
        dates_before = [d for d in all_holdings if d <= div_date and ticker in all_holdings[d]]
        if not dates_before:
            continue
        latest_date = max(dates_before)
        shares_held = all_holdings[latest_date][ticker]
        if shares_held <= 0:
            continue
        dividend_value = shares_held * amount
        for cash_date in sorted(all_cash):
            if cash_date >= div_date:
                all_cash[cash_date].balance += dividend_value
    db.session.commit()

def fetch_longnames_and_sectors(tickers):
    info_map = {}
    for ticker in tickers:
        try:
            info = yf.Ticker(ticker).info
            longname = info.get("longName") or info.get("shortName") or ticker
            sector = info.get("sector") or "Unknown"
            info_map[ticker] = {"longname": longname, "sector": sector}
        except Exception:
            info_map[ticker] = {"longname": ticker, "sector": "Unknown"}
    return info_map

def generate_holdings(transactions, from_date=None, to_date=None, starting_cash=None):
    start = from_date or DEFAULT_START_DATE
    end = to_date or date.today()
    holdings_by_ticker = defaultdict(int)
    holdings_by_date = defaultdict(lambda: defaultdict(int))
    cash_by_date = {}
    tx_by_date = defaultdict(list)
    dividend_by_date = defaultdict(list)
    for tx_date, ticker, tx_type, amount in transactions:
        tx_by_date[tx_date].append((ticker, tx_type, amount))
    if from_date:
        prev_date = from_date - timedelta(days=1)
        for h in Holding.query.filter_by(date=prev_date).all():
            holdings_by_ticker[h.ticker] = h.shares
    for div in Dividend.query.filter(Dividend.date >= start, Dividend.date <= end).all():
        dividend_by_date[div.date].append(div)
    tickers = {tx[1] for tx in transactions}
    price_entries = HistoricalPrice.query.filter(HistoricalPrice.ticker.in_(tickers), HistoricalPrice.date >= start, HistoricalPrice.date <= end).all()
    price_lookup = {(p.ticker, p.date): p.close for p in price_entries}
    current_cash = starting_cash if from_date and starting_cash is not None else STARTING_CASH
    current_date = start
    while current_date <= end:
        for ticker, tx_type, amount in tx_by_date.get(current_date, []):
            try:
                total_value = amount * price_lookup.get((ticker, current_date), 0)
            except:
                print(f"Error fetching price for {ticker} on {current_date}")
                print(amount)
                print(price_lookup.get((ticker, current_date), 0))

            if total_value == 0:
                continue
            if tx_type.lower() == "buy":
                holdings_by_ticker[ticker] += amount
                current_cash -= total_value
            elif tx_type.lower() == "sell":
                holdings_by_ticker[ticker] -= amount
                current_cash += total_value
        for div in dividend_by_date.get(current_date, []):
            shares = holdings_by_ticker.get(div.ticker, 0)
            if shares > 0:
                current_cash += shares * div.amount
        for ticker, shares in holdings_by_ticker.items():
            if shares > 0:
                holdings_by_date[current_date][ticker] = shares
        cash_by_date[current_date] = current_cash
        current_date += timedelta(days=1)
    info_map = fetch_longnames_and_sectors(tickers)
    for dt, tickers_dict in holdings_by_date.items():
        if Holding.query.filter_by(date=dt).first():
            continue
        for ticker, shares in tickers_dict.items():
            db.session.add(Holding(ticker=ticker, date=dt, shares=shares, longname=info_map[ticker]["longname"], sector=info_map[ticker]["sector"]))
    for dt, balance in cash_by_date.items():
        if Cash.query.filter_by(date=dt).first():
            continue
        db.session.add(Cash(date=dt, balance=balance))
    db.session.commit()

def load_unique_tickers(csv_path):
    tickers = set()
    with open(csv_path, newline='', encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            ticker = row['Ticker'].strip()
            if ticker:
                tickers.add(ticker.upper())
    return list(tickers)

def bootstrap_data():
    transactions, price_points = load_transactions(CSV_PATH)
    tickers = {tx[1] for tx in transactions}
    for ticker in tickers:
        update_close_prices(ticker, DEFAULT_START_DATE)
    interpolate_and_add_prices(price_points)
    generate_holdings(transactions)
    for ticker in tickers:
        update_close_prices(ticker, DEFAULT_START_DATE, include_dividends=True)
    apply_dividends_to_cash()
