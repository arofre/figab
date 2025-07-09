from flask import Flask, render_template, request
from models import db, Holding, HistoricalPrice, Cash, Dividend
from bootstrap import bootstrap_data
from update import update_close_prices
from sqlalchemy import select
import pandas as pd
from datetime import datetime, timedelta
import plotly.express as px
import json

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///prices.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

with app.app_context():
    db.create_all()
    if db.session.query(HistoricalPrice).count() == 0:
        bootstrap_data()


def get_portfolio_value_series():
    # Load all relevant data at once

    holdings = pd.read_sql(select(Holding), db.engine)
    prices = pd.read_sql(select(HistoricalPrice), db.engine)
    cash = pd.read_sql(select(Cash), db.engine)
    dividends = pd.read_sql(select(Dividend), db.engine)


    # Ensure proper datetime parsing
    holdings['date'] = pd.to_datetime(holdings['date'])
    prices['date'] = pd.to_datetime(prices['date'])
    cash['date'] = pd.to_datetime(cash['date'])
    dividends['date'] = pd.to_datetime(dividends['date'])

    # Prepare historical prices: get latest price <= holding date
    prices = prices.sort_values(['ticker', 'date'])
    latest_prices = (
        holdings
        .merge(prices, on='ticker', how='left')
        .query('date_y <= date_x')
        .sort_values(['ticker', 'date_x', 'date_y'])
        .drop_duplicates(subset=['ticker', 'date_x'], keep='last')
        .rename(columns={'date_x': 'date', 'close': 'price'})
        [['ticker', 'date', 'shares', 'price']]
    )

    latest_prices['value'] = latest_prices['shares'] * latest_prices['price']

    # Portfolio value from holdings
    holding_value = (
        latest_prices.groupby('date')['value'].sum().rename("holdings_value")
    )

    # Portfolio cash
    cash_value = cash.groupby('date')['balance'].sum().rename("cash_value")

    # Dividend cash (dividend.amount * shares)
    if not dividends.empty:
        dividends = dividends.merge(holdings, on=['date', 'ticker'], how='left')
        dividends['div_value'] = dividends['amount'] * dividends['shares']
        dividend_cash = dividends.groupby('date')['div_value'].sum().rename("div_value")
    else:
        dividend_cash = pd.Series(dtype='float64')

    # Combine all components
    df = pd.concat([holding_value, cash_value, dividend_cash], axis=1).fillna(0)
    df['total_value'] = df.sum(axis=1)

    df = df[['total_value']].sort_index()
    df.index = pd.to_datetime(df.index)

    return df['total_value']


from sqlalchemy import func, desc
import pandas as pd

def get_allocation(latest_date):
    holdings = Holding.query.filter_by(date=latest_date).all()
    if not holdings:
        return pd.DataFrame(columns=['ticker', 'value'])

    tickers = [h.ticker for h in holdings]

    subq = (
        db.session.query(
            HistoricalPrice.ticker,
            func.max(HistoricalPrice.date).label('max_date')
        )
        .filter(
            HistoricalPrice.ticker.in_(tickers),
            HistoricalPrice.date <= latest_date
        )
        .group_by(HistoricalPrice.ticker)
        .subquery()
    )

    latest_prices = (
        db.session.query(HistoricalPrice.ticker, HistoricalPrice.close)
        .join(subq, (HistoricalPrice.ticker == subq.c.ticker) & (HistoricalPrice.date == subq.c.max_date))
        .all()
    )

    price_dict = {tp[0]: tp[1] for tp in latest_prices}

    data = []
    for h in holdings:
        price = price_dict.get(h.ticker)
        if price is not None:
            value = price * h.shares
            data.append({'ticker': h.ticker, 'value': value})

    return pd.DataFrame(data)


from apscheduler.schedulers.background import BackgroundScheduler
from update import update_close_prices

import os 
from flask import current_app

from flask import request, Response

def check_auth(username, password):
    return username == 'admin' and password == 'Knarkpengar1'

def authenticate():
    return Response(
        'Could not verify your access level for that URL.\n'
        'You have to login with proper credentials', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'})

@app.route("/reset_db")
def reset_db():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    scheduled_daily_update()
    return "Database reset done"



from sqlalchemy import select, func
import pandas as pd
from datetime import timedelta

@app.route("/")
def dashboard():
    all_dates = db.session.query(Holding.date).distinct().order_by(Holding.date).all()
    all_dates = [d[0] for d in all_dates]

    if not all_dates:
        return "No data available"

    portfolio_values = []
    for dt in all_dates:
        holdings = Holding.query.filter_by(date=dt).all()
        cash_entry = Cash.query.filter_by(date=dt).first()
        cash = cash_entry.balance if cash_entry else 0

        dividends = Dividend.query.filter_by(date=dt).all()
        for div in dividends:
            holding = next((h for h in holdings if h.ticker == div.ticker), None)
            if holding:
                cash += div.amount * holding.shares

        tickers = [h.ticker for h in holdings]
        if tickers:
            subq = (
                db.session.query(
                    HistoricalPrice.ticker,
                    func.max(HistoricalPrice.date).label("max_date"),
                )
                .filter(
                    HistoricalPrice.ticker.in_(tickers),
                    HistoricalPrice.date <= dt,
                )
                .group_by(HistoricalPrice.ticker)
                .subquery()
            )

            latest_prices = (
                db.session.query(HistoricalPrice.ticker, HistoricalPrice.close)
                .join(
                    subq,
                    (HistoricalPrice.ticker == subq.c.ticker)
                    & (HistoricalPrice.date == subq.c.max_date),
                )
                .all()
            )
            price_dict = {p[0]: p[1] for p in latest_prices}
        else:
            price_dict = {}

        total_value = cash
        for h in holdings:
            price = price_dict.get(h.ticker)
            if price:
                total_value += price * h.shares

        portfolio_values.append((dt, total_value))

    series = pd.Series(dict(portfolio_values))
    series.index = pd.to_datetime(series.index)

    latest = series.index[-1]
    first = series.index[0]
    today_val = series.iloc[-1]

    def percent_change(start_date):
        s = series[series.index <= start_date]
        if s.empty:
            return None
        return ((today_val - s.iloc[-1]) / s.iloc[-1]) * 100

    now = pd.Timestamp.now().normalize()
    pct_changes = {
        "Today": percent_change(now - timedelta(days=1)),
        "This Week": percent_change(now - timedelta(days=7)),
        "This Month": percent_change(now.replace(day=1)),
        "This Year": percent_change(now.replace(month=1, day=1)),
        "All Time": percent_change(first),
    }

    df_series = series.reset_index()
    df_series.columns = ["date", "value"]
    df_series["date"] = pd.to_datetime(df_series["date"])

    labels = df_series["date"].dt.strftime("%Y-%m-%d").tolist()
    data_values = df_series["value"].tolist()

    df_alloc = get_allocation(latest)
    if df_alloc.empty:
        alloc_labels = []
        alloc_values = []
    else:
        alloc_labels = df_alloc["ticker"].tolist()
        alloc_values = df_alloc["value"].tolist()

    y_max = round(df_series["value"].max() * 1.05, -4)
    y_min = round(df_series["value"].min() * 0.95, -4)

    return render_template(
        "dashboard.html",
        pct_changes=pct_changes,
        latest_value=round(today_val, 2),
        line_labels=labels,
        line_data=data_values,
        alloc_labels=alloc_labels,
        alloc_values=alloc_values,
        y_max=y_max,
        y_min=y_min,
    )


def scheduled_daily_update():
    with app.app_context():
        db.drop_all()
        db.create_all()
        bootstrap_data()


scheduler = BackgroundScheduler()
scheduler.add_job(scheduled_daily_update, 'cron', hour=1, minute=00)
scheduler.start()

if __name__ == "__main__":
    try:
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()