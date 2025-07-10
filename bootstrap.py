import csv
from datetime import date, timedelta
from collections import defaultdict
from models import db, Holding
from update import update_close_prices
from models import Cash, HistoricalPrice, Dividend

CSV_PATH = "transactions.csv"
DEFAULT_START_DATE = date(2025, 2, 17)
STARTING_CASH = 150_000  # SEK

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

def generate_holdings(transactions, from_date=None, to_date=None, starting_cash=None):
    """
    Simulate holdings and cash between from_date and to_date.
    If from_date/to_date not provided, defaults to full range starting at DEFAULT_START_DATE to today.
    starting_cash must be provided when from_date is set.
    """
    # Determine simulation bounds
    if from_date is None:
        start = DEFAULT_START_DATE
    else:
        start = from_date
    end = to_date or date.today()

    # Initialize holdings & cash
    holdings_by_ticker = defaultdict(int)
    holdings_by_date = defaultdict(lambda: defaultdict(int))
    cash_by_date = {}
    tx_by_date = defaultdict(list)
    dividend_by_date = defaultdict(list)

    # Build transaction lookup
    for tx_date, ticker, tx_type, amount in transactions:
        tx_by_date[tx_date].append((ticker, tx_type, amount))

    # Pre-load existing holdings as of the day before 'start'
    if from_date:
        prev_date = from_date - timedelta(days=1)
        for h in Holding.query.filter_by(date=prev_date).all():
            holdings_by_ticker[h.ticker] = h.shares

    # Build dividend lookup for the period
    for div in Dividend.query.filter(Dividend.date >= start, Dividend.date <= end).all():
        dividend_by_date[div.date].append(div)

    # Starting cash
    if from_date and starting_cash is not None:
        current_cash = starting_cash
    else:
        current_cash = STARTING_CASH

    current_date = start
    # Iterate dates
    while current_date <= end:
        # Apply transactions
        for ticker, tx_type, amount in tx_by_date.get(current_date, []):
            entry = HistoricalPrice.query.filter_by(ticker=ticker, date=current_date).first()
            if not entry:
                continue
            total_value = amount * entry.close
            if tx_type.lower() == "buy":
                holdings_by_ticker[ticker] += amount
                current_cash -= total_value
            elif tx_type.lower() == "sell":
                holdings_by_ticker[ticker] -= amount
                current_cash += total_value

        # Apply dividends
        for div in dividend_by_date.get(current_date, []):
            shares = holdings_by_ticker.get(div.ticker, 0)
            if shares > 0:
                current_cash += shares * div.amount

        # Record holdings
        for ticker, shares in holdings_by_ticker.items():
            if shares > 0:
                holdings_by_date[current_date][ticker] = shares

        # Record cash
        cash_by_date[current_date] = current_cash
        current_date += timedelta(days=1)

    # Persist to DB only for the new date range
    for dt, tickers in holdings_by_date.items():
        # Skip if row already exists
        if Holding.query.filter_by(date=dt).first():
            continue
        for ticker, shares in tickers.items():
            db.session.add(Holding(ticker=ticker, date=dt, shares=shares))

    for dt, balance in cash_by_date.items():
        if Cash.query.filter_by(date=dt).first():
            continue
        db.session.add(Cash(date=dt, balance=balance))

    db.session.commit()
    print(f"Holdings & Cash populated for dates {start} to {end}")

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

    print("Fetching historical prices...")
    for ticker in tickers:
        update_close_prices(ticker, DEFAULT_START_DATE)

    print("Adding synthetic prices (if defined)...")
    interpolate_and_add_prices(price_points)

    print("Populating holdings...")
    generate_holdings(transactions)
    
    print("Re-fetching dividends after holdings exist...")
    for ticker in tickers:
        update_close_prices(ticker, DEFAULT_START_DATE, include_dividends=True)

    print("Applying dividends to cash balances...")
    apply_dividends_to_cash()
