from flask import Flask, render_template, request, send_file
from models import db, Holding, HistoricalPrice, Cash, Dividend
from bootstrap import bootstrap_data
from update import update_close_prices
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import io
import os
from datetime import datetime, timedelta
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///prices.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

with app.app_context():
    db.create_all()
    if db.session.query(HistoricalPrice).count() == 0:
        print("Bootstrapping database...")
        bootstrap_data()
    else:
        print("Database already populated.")


def get_portfolio_value_series():
    all_dates = db.session.query(Holding.date).distinct().order_by(Holding.date).all()
    all_dates = [d[0] for d in all_dates]
    portfolio_values = []

    for dt in all_dates:
        holdings = Holding.query.filter_by(date=dt).all()
        cash_entry = Cash.query.filter_by(date=dt).first()
        cash = cash_entry.balance if cash_entry else 0

        dividends = Dividend.query.filter_by(date=dt).all()
        for div in dividends:
            holding = Holding.query.filter_by(ticker=div.ticker, date=dt).first()
            if holding:
                cash += div.amount * holding.shares

        total_value = cash
        for h in holdings:
            price_entry = HistoricalPrice.query.filter_by(ticker=h.ticker, date=dt).first()
            if not price_entry:
                price_entry = HistoricalPrice.query.filter(HistoricalPrice.ticker == h.ticker, HistoricalPrice.date <= dt).order_by(HistoricalPrice.date.desc()).first()
            if price_entry:
                total_value += price_entry.close * h.shares

        portfolio_values.append((dt, total_value))

    return pd.Series(dict(portfolio_values))


def plot_value_chart(series):
    plt.figure(figsize=(12, 6))
    plt.plot(series.index, series.values, marker='o', linestyle='-', color='blue')
    plt.title("Total Portfolio Value Over Time")
    plt.xlabel("Date")
    plt.ylabel("Value (SEK)")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig("static/value_chart.png")
    plt.close()


def plot_allocation_pie(latest_date):
    holdings = Holding.query.filter_by(date=latest_date).all()
    labels, sizes = [], []

    for h in holdings:
        price = HistoricalPrice.query.filter(HistoricalPrice.ticker == h.ticker, HistoricalPrice.date <= latest_date).order_by(HistoricalPrice.date.desc()).first()
        if price:
            labels.append(h.ticker)
            sizes.append(price.close * h.shares)

    if sizes:
        plt.figure(figsize=(6, 6))
        plt.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=140)
        plt.title("Portfolio Allocation by Ticker")
        plt.tight_layout()
        plt.savefig("static/allocation_chart.png")
        plt.close()


def scheduled_update():
    with app.app_context():
        print(f"[{datetime.now()}] Running scheduled update...")

        transaction_file = "transactions.csv"
        latest_holding = db.session.query(Holding.date).order_by(Holding.date.desc()).first()
        latest_db_date = latest_holding[0] if latest_holding else None

        if os.path.exists(transaction_file):
            file_mod_time = datetime.fromtimestamp(os.path.getmtime(transaction_file))
            if not latest_db_date or file_mod_time.date() > latest_db_date:
                print("Detected new transactions. Rebootstrapping data...")
                bootstrap_data()
            else:
                print("No new transactions detected.")
        else:
            print("Transaction file not found.")

        tickers = db.session.query(HistoricalPrice.ticker).distinct().all()
        for (ticker,) in tickers:
            update_close_prices(ticker)

        print("Scheduled update completed.")


@app.route("/")
def dashboard():
    series = get_portfolio_value_series()
    if series.empty:
        return "No data available"

    latest = series.index[-1]
    first = series.index[0]
    today_val = series[-1]

    def percent_change(start_date):
        s = series[series.index <= start_date]
        if s.empty:
            return None
        return ((today_val - s[-1]) / s[-1]) * 100

    now = datetime.now().date()
    pct_changes = {
        "Idag": percent_change(now - timedelta(days=1)),
        "Senaste veckan": percent_change(now - timedelta(days=7)),
        "Senaste Månaden": percent_change(now.replace(day=1)),
        "Detta År": percent_change(now.replace(month=1, day=1)),
        "Sedan start": percent_change(first),
    }

    return render_template("dashboard.html", 
                           pct_changes=pct_changes,
                           latest_value=round(today_val, 2),
                           value_chart_url="/value_chart.png",
                           allocation_chart_url="/allocation_chart.png")



@app.route("/update", methods=["POST"])
def update():
    ticker = request.args.get("ticker")
    if not ticker:
        return "Missing 'ticker'", 400

    update_close_prices(ticker)
    return f"Updated {ticker.upper()}"

@app.route('/value_chart.png')
def value_chart():
    series = get_portfolio_value_series()
    if series.empty:
        return "No data", 404

    plt.figure(figsize=(12, 6))
    plt.plot(series.index, series.values, marker='o', linestyle='-', color='blue')
    plt.title("Total Portfolio Value Over Time")
    plt.xlabel("Date")
    plt.ylabel("Value (SEK)")
    plt.grid(True)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    plt.close()
    buf.seek(0)

    return send_file(buf, mimetype='image/png')


@app.route('/allocation_chart.png')
def allocation_chart():
    latest_date = db.session.query(Holding.date).order_by(Holding.date.desc()).first()
    if not latest_date:
        return "No data", 404

    latest_date = latest_date[0]

    holdings = Holding.query.filter_by(date=latest_date).all()
    labels, sizes = [], []

    for h in holdings:
        price = HistoricalPrice.query.filter(HistoricalPrice.ticker == h.ticker, HistoricalPrice.date <= latest_date).order_by(HistoricalPrice.date.desc()).first()
        if price:
            labels.append(h.ticker)
            sizes.append(price.close * h.shares)

    if not sizes:
        return "No data", 404

    plt.figure(figsize=(6, 6))
    plt.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=140)
    plt.title("Fördelning innehav")
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    plt.close()
    buf.seek(0)

    return send_file(buf, mimetype='image/png')


@app.route("/portfolio_value")
def portfolio_value_image():
    series = get_portfolio_value_series()
    if series.empty:
        return "No data"

    plt.figure(figsize=(12, 6))
    plt.plot(series.index, series.values, marker='o', linestyle='-', color='blue')
    plt.title("Utveckling FIGAB, OMXS30gi, S&P100")
    plt.xlabel("Datum")
    plt.ylabel("Värde (SEK)")
    plt.grid(True)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png")
    plt.close()
    buf.seek(0)

    return send_file(buf, mimetype='image/png')


scheduler = BackgroundScheduler()
scheduler.add_job(func=scheduled_update, trigger='cron', hour=0, minute=0)
scheduler.start()

atexit.register(lambda: scheduler.shutdown())


if __name__ == "__main__":
    app.run(debug=True)
