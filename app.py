from datetime import datetime, timedelta, date
import os

import pandas as pd
from flask import Flask, render_template, request, Response, redirect, url_for, flash
from sqlalchemy import func, select
from apscheduler.schedulers.background import BackgroundScheduler

from models import db, Holding, HistoricalPrice, Cash, Dividend
from bootstrap import bootstrap_data, load_transactions, generate_holdings, DEFAULT_START_DATE, STARTING_CASH
from update import update_close_prices


app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///prices.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

app.secret_key = os.environ.get("SECRET_KEY", "fallback-dev-key")

db.init_app(app)


with app.app_context():
    db.create_all()
    if db.session.query(HistoricalPrice).count() == 0:
        bootstrap_data()


def get_latest_prices_for_holdings(date, tickers):
    if not tickers:
        return {}

    subq = (
        db.session.query(
            HistoricalPrice.ticker,
            func.max(HistoricalPrice.date).label('max_date')
        )
        .filter(
            HistoricalPrice.ticker.in_(tickers),
            HistoricalPrice.date <= date
        )
        .group_by(HistoricalPrice.ticker)
        .subquery()
    )

    latest_prices = (
        db.session.query(HistoricalPrice.ticker, HistoricalPrice.close)
        .join(subq, (HistoricalPrice.ticker == subq.c.ticker) & (HistoricalPrice.date == subq.c.max_date))
        .all()
    )

    return {ticker: price for ticker, price in latest_prices}

@app.route("/increment")
def incremental_update():
    """Fetch only new data since our last date and append holdings/cash"""
    with app.app_context():
        last_price_date = db.session.query(func.max(HistoricalPrice.date)).scalar()
        start_date = (last_price_date + timedelta(days=1)) if last_price_date else DEFAULT_START_DATE

        tickers = [t[0] for t in db.session.query(HistoricalPrice.ticker).distinct().all()]
        for ticker in tickers:
            update_close_prices(ticker, start_date=start_date)

        last_holding_date = db.session.query(func.max(Holding.date)).scalar()
        txs, _ = load_transactions("transactions.csv")
        from_date = (last_holding_date + timedelta(days=1)) if last_holding_date else DEFAULT_START_DATE
        to_date = date.today()
        starting_cash = (db.session.query(Cash.balance)
                         .filter(Cash.date == last_holding_date)
                         .scalar()) if last_holding_date else STARTING_CASH

        generate_holdings(
            transactions=txs,
            from_date=from_date,
            to_date=to_date,
            starting_cash=starting_cash
        )



    return redirect(url_for('dashboard'))

def get_index_prices(tickers, dates):
    prices = (
        db.session.query(HistoricalPrice.ticker, HistoricalPrice.date, HistoricalPrice.close)
        .filter(HistoricalPrice.ticker.in_(tickers))
        .filter(HistoricalPrice.date.in_(dates))
        .all()
    )

    df = pd.DataFrame(prices, columns=['ticker', 'date', 'close'])
    df['date'] = pd.to_datetime(df['date'])
    return df


def get_allocation(latest_date):
    holdings = Holding.query.filter_by(date=latest_date).all()
    if not holdings:
        return pd.DataFrame(columns=['ticker', 'value'])

    tickers = [h.ticker for h in holdings]
    price_dict = get_latest_prices_for_holdings(latest_date, tickers)

    data = []
    for h in holdings:
        price = price_dict.get(h.ticker)
        if price is not None:
            data.append({'ticker': h.ticker, 'value': price * h.shares})

    return pd.DataFrame(data)


def calculate_portfolio_values():
    holdings = pd.read_sql(select(Holding), db.engine)
    prices = pd.read_sql(select(HistoricalPrice), db.engine)
    cash = pd.read_sql(select(Cash), db.engine)
    dividends = pd.read_sql(select(Dividend), db.engine)

    for df in [holdings, prices, cash, dividends]:
        if not df.empty:
            df['date'] = pd.to_datetime(df['date'])

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

    holding_value = latest_prices.groupby('date')['value'].sum().rename("holdings_value")
    cash_value = cash.groupby('date')['balance'].sum().rename("cash_value")

    if not dividends.empty:
        dividends = dividends.merge(holdings, on=['date', 'ticker'], how='left')
        dividends['div_value'] = dividends['amount'] * dividends['shares']
        dividend_cash = dividends.groupby('date')['div_value'].sum().rename("div_value")
    else:
        dividend_cash = pd.Series(dtype='float64')

    df = pd.concat([holding_value, cash_value, dividend_cash], axis=1).fillna(0)
    df['total_value'] = df.sum(axis=1)

    return df['total_value'].sort_index()


def percent_change(series, start_date, today_val):
    s = series[series.index <= start_date]
    if s.empty:
        return None
    return ((today_val - s.iloc[-1]) / s.iloc[-1]) * 100


def check_auth(username, password):
    return username == 'admin' and password == app.secret_key


def authenticate():
    return Response(
        'Could not verify your access level for that URL.\n'
        'You have to login with proper credentials', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'}
    )


@app.route("/")
def dashboard():
    all_dates = [d[0] for d in db.session.query(Holding.date).distinct().order_by(Holding.date).all()]
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
        price_dict = get_latest_prices_for_holdings(dt, tickers)

        total_value = cash
        for h in holdings:
            price = price_dict.get(h.ticker)
            if price:
                total_value += price * h.shares

        portfolio_values.append((dt, total_value))

    series = pd.Series(dict(portfolio_values))
    series.index = pd.to_datetime(series.index)

    latest_date = series.index[-1]
    first_date = series.index[0]
    today_val = series.iloc[-1]

    now = pd.Timestamp.now().normalize()

    pct_changes = {
        "This Week": percent_change(series, now - timedelta(days=7), today_val),
        "This Month": percent_change(series, now.replace(day=1), today_val),
        "This Year": percent_change(series, now.replace(month=1, day=1), today_val),
        "All Time": percent_change(series, first_date, today_val),
    }

    df_series = series.reset_index()
    df_series.columns = ["date", "value"]
    df_series["date"] = pd.to_datetime(df_series["date"])

    index_tickers = ['^OMX', '^GSPC']
    index_prices_df = get_index_prices(index_tickers, series.index)

    index_pivot = index_prices_df.pivot(index='date', columns='ticker', values='close')

    omx_data_temp = index_pivot.get('^OMX', pd.Series()).reindex(series.index).fillna(method='ffill')
    omx_data = [x * 150000 / omx_data_temp[0] for x in omx_data_temp]

    gspc_data_temp = index_pivot.get('^GSPC', pd.Series()).reindex(series.index).fillna(method='ffill')
    gspc_data = [x * 150000 / gspc_data_temp[0] for x in gspc_data_temp]

    line_labels = df_series["date"].dt.strftime("%Y-%m-%d").tolist()
    line_data = df_series["value"].tolist()

    df_alloc = get_allocation(latest_date)

    if not df_alloc.empty:
        total_value = df_alloc['value'].sum()

        df_alloc['percent'] = (df_alloc['value'] / total_value) * 100
        alloc_labels = df_alloc["ticker"].tolist()
        alloc_values = df_alloc["percent"].tolist()
    else:
        alloc_labels = []
        alloc_values = []


    y_max = round(df_series["value"].max() * 1.05, -4)
    y_min = round(df_series["value"].min() * 0.95, -4)

    return render_template(
        "dashboard.html",
        pct_changes=pct_changes,
        latest_value=round(today_val, 2),
        line_labels=line_labels,
        line_data=line_data,
        omx_data=omx_data,
        gspc_data=gspc_data,
        alloc_labels=alloc_labels,
        alloc_values=alloc_values,
        y_max=y_max,
        y_min=y_min,
        cash=round(cash),
    )


@app.route("/reset_db")
def reset_db():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()

    reset_everything()
    return redirect(url_for('dashboard'))


@app.route("/admin", methods=["GET", "POST"])
def admin_dashboard():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()

    if request.method == "POST":
        ticker = request.form.get("ticker", "").strip().upper()
        amount = request.form.get("amount", "").strip()
        action = request.form.get("action")
        date_str = request.form.get("date")

        if not ticker or not amount or action not in ("Buy", "Sell"):
            flash("Please fill out all fields correctly.")
            return redirect(url_for("admin_dashboard"))

        try:
            amount = int(amount)
        except ValueError:
            flash("Amount must be an integer.")
            return redirect(url_for("admin_dashboard"))

        line = f"{ticker};{date_str};{action};{amount};\n"

        with open("transactions.csv", "a") as f:
            f.write(line)

        flash("Transaction recorded successfully.")
        return redirect(url_for("admin_dashboard"))

    return render_template("admin.html")

import discord
from discord.ext import commands
from flask import Flask
from models import db, Holding, Cash
import os

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

app = Flask(__name__)

def get_portfolio_message():
    with app.app_context():
        all_dates = [d[0] for d in db.session.query(Holding.date).distinct().order_by(Holding.date).all()]
        if len(all_dates) < 2:
            return "Not enough data"

        last_date = all_dates[-1]
        prev_date = all_dates[-2]

        holdings_last = Holding.query.filter_by(date=last_date).all()
        tickers_last = [h.ticker for h in holdings_last]
        price_dict_last = get_latest_prices_for_holdings(last_date, tickers_last)

        total_last = sum(price_dict_last.get(h.ticker, 0) * h.shares for h in holdings_last)
        cash_last = Cash.query.filter_by(date=last_date).first()
        if cash_last:
            total_last += cash_last.balance

        holdings_prev = Holding.query.filter_by(date=prev_date).all()
        tickers_prev = [h.ticker for h in holdings_prev]
        price_dict_prev = get_latest_prices_for_holdings(prev_date, tickers_prev)

        total_prev = sum(price_dict_prev.get(h.ticker, 0) * h.shares for h in holdings_prev)
        cash_prev = Cash.query.filter_by(date=prev_date).first()
        if cash_prev:
            total_prev += cash_prev.balance

        message = (
            f"Värdet av FIGAB {last_date}:\n"
            f"SEK {total_last:,.2f}\n"
        )
        return message


@bot.command(name="portfolio")
async def portfolio(ctx):
    msg = get_portfolio_message()
    await ctx.send(msg)


if __name__ == "__main__":
    bot.run(TOKEN)


def reset_everything():
    with app.app_context():
        db.drop_all()
        db.create_all()
        bootstrap_data()



scheduler = BackgroundScheduler()

scheduler.add_job(incremental_update, 'cron', hour=23, minute=0)

#scheduler.add_job(
#    lambda: asyncio.run_coroutine_threadsafe(send_portfolio_update(), client.loop),
#    CronTrigger(hour=8, minute=00, timezone=timezone("Europe/Stockholm"))
#)

scheduler.start()

import threading

if __name__ == "__main__":
    #discord_thread = threading.Thread(target=lambda: bot.run(TOKEN), daemon=True)
    #discord_thread.start()

    try:
        port = int(os.environ.get("PORT", 8080))
        app.run(host="0.0.0.0", port=port)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
