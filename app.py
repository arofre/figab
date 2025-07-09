from flask import Flask, render_template, request
from models import db, Holding, HistoricalPrice, Cash, Dividend
from bootstrap import bootstrap_data
from update import update_close_prices

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
                price_entry = HistoricalPrice.query.filter(
                    HistoricalPrice.ticker == h.ticker,
                    HistoricalPrice.date <= dt
                ).order_by(HistoricalPrice.date.desc()).first()
            if price_entry:
                total_value += price_entry.close * h.shares

        portfolio_values.append((dt, total_value))

    series = pd.Series(dict(portfolio_values))
    series.index = pd.to_datetime(series.index)
    return series


def get_allocation(latest_date):
    holdings = Holding.query.filter_by(date=latest_date).all()
    data = []
    for h in holdings:
        price = HistoricalPrice.query.filter(
            HistoricalPrice.ticker == h.ticker,
            HistoricalPrice.date <= latest_date
        ).order_by(HistoricalPrice.date.desc()).first()
        if price:
            value = price.close * h.shares
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

    now = pd.Timestamp.now().normalize()
    pct_changes = {
        "Today": percent_change(now - timedelta(days=1)),
        "This Week": percent_change(now - timedelta(days=7)),
        "This Month": percent_change(now.replace(day=1)),
        "This Year": percent_change(now.replace(month=1, day=1)),
        "All Time": percent_change(first),
    }

    df_series = series.reset_index()
    df_series.columns = ['date', 'value']
    df_series['date'] = pd.to_datetime(df_series['date'])
    
    labels = df_series['date'].dt.strftime('%Y-%m-%d').tolist()
    data_values = df_series['value'].tolist()

    # Allocation pie chart data
    df_alloc = get_allocation(latest)
    if df_alloc.empty:
        alloc_labels = []
        alloc_values = []
    else:
        alloc_labels = df_alloc['ticker'].tolist()
        alloc_values = df_alloc['value'].tolist()

    return render_template("dashboard.html",
        pct_changes=pct_changes,
        latest_value=round(today_val, 2),
        line_labels=labels,
        line_data=data_values,
        alloc_labels=alloc_labels,
        alloc_values=alloc_values,
        y_max=round(df_series["value"].max()*1.05, -4),
        y_min=round(df_series["value"].min()*0.95, -4)
    )

def scheduled_daily_update():
    with app.app_context():
        db.drop_all()
        db.create_all()
        bootstrap_data()


scheduler = BackgroundScheduler()
scheduler.add_job(scheduled_daily_update, 'cron', hour=00, minute=00)  # runs daily at 00:00 UTC
scheduler.start()

if __name__ == "__main__":
    try:
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()