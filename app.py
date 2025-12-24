from datetime import datetime, timedelta, date
import os
import json

import numpy as np
import pandas as pd
from flask import Flask, render_template, request, Response, redirect, url_for, flash, send_from_directory
from sqlalchemy import func, select
from apscheduler.schedulers.background import BackgroundScheduler
from collections import defaultdict

from models import db, Holding, HistoricalPrice, Cash, Dividend
from bootstrap import bootstrap_data, load_transactions, generate_holdings, DEFAULT_START_DATE, STARTING_CASH
from update import update_close_prices


app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///prices.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
UPLOAD_FOLDER = os.path.join(app.root_path, 'static/reports')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.secret_key = os.environ.get("SECRET_KEY", "fallback-dev-key")

db.init_app(app)


with app.app_context():
    db.create_all()
    if db.session.query(HistoricalPrice).count() == 0:
        bootstrap_data()


def beta_ratio(asset_prices, benchmark_prices):
    asset_prices = np.array(asset_prices)
    benchmark_prices = np.array(benchmark_prices)
    if len(asset_prices) != len(benchmark_prices) or len(asset_prices) < 2:
        return np.nan
    return np.cov(asset_prices, benchmark_prices)[0, 1] / np.var(benchmark_prices)

def sharpe_ratio(prices):
    prices = np.array(prices)
    returns = np.diff(prices) / prices[:-1]
    if len(returns) < 2:
        return np.nan
    return np.mean(returns) / np.std(returns, ddof=1) * np.sqrt(252)

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

        compute_dashboard_data()

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

def get_allocation_by_sector(latest_date):
    holdings = Holding.query.filter_by(date=latest_date).all()
    if not holdings:
        return pd.DataFrame(columns=['sector', 'value'])

    data = []
    for h in holdings:
        if h.sector:
            price = get_latest_prices_for_holdings(latest_date, [h.ticker]).get(h.ticker)
            if price:
                data.append({'sector': h.sector, 'value': price * h.shares})

    df = pd.DataFrame(data)
    if not df.empty:
        df = df.groupby('sector')['value'].sum().reset_index()
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


def calculate_portfolio_values_optimized():
    holdings = pd.read_sql(select(Holding), db.engine)
    prices = pd.read_sql(select(HistoricalPrice), db.engine)
    cash = pd.read_sql(select(Cash), db.engine)
    dividends = pd.read_sql(select(Dividend), db.engine)

    for df in [holdings, prices, cash, dividends]:
        if not df.empty:
            df['date'] = pd.to_datetime(df['date'])

    if holdings.empty or prices.empty:
        return pd.Series(dtype='float64')

    prices = prices.sort_values(['ticker', 'date'])
    merged = holdings.merge(prices, on='ticker', how='left')
    merged = merged[merged['date_y'] <= merged['date_x']]
    merged = merged.sort_values(['ticker', 'date_x', 'date_y'])
    merged = merged.drop_duplicates(subset=['ticker', 'date_x'], keep='last')
    merged = merged.rename(columns={'date_x': 'date', 'close': 'price'})
    merged['value'] = merged['shares'] * merged['price']

    holding_value = merged.groupby('date')['value'].sum().rename("holdings_value")
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

def get_current_holdings_longnames():
    latest_date = Holding.query.order_by(Holding.date.desc()).first()
    if not latest_date:
        return []
    holdings = Holding.query.filter_by(date=latest_date.date).all()
    return list({h.longname for h in holdings if h.longname})

def get_past_holdings_longnames(current_holdings):
    all_holdings = Holding.query.all()
    return list({h.longname for h in all_holdings if h.longname and h.longname not in current_holdings})


def check_auth(username, password):
    return username == 'admin' and password == app.secret_key


def authenticate():
    return Response(
        'Could not verify your access level for that URL.\n'
        'You have to login with proper credentials', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'}
    )

@app.route("/delete_report", methods=["POST"])
def delete_report():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    filename = request.form.get("delete_file", "").strip()
    if not filename:
        flash("Please provide a filename.")
        return redirect(url_for("admin_dashboard"))

    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)

    if os.path.exists(file_path):
        os.remove(file_path)
        flash(f"File '{filename}' deleted successfully.")
    else:
        flash(f"File '{filename}' does not exist.")
    
    return redirect(url_for("admin_dashboard"))

@app.route("/reports/<path:filename>", endpoint="custom_reports")
def reports(filename):
    return send_from_directory("static/reports", filename)

@app.route("/")
def dashboard():
    cache_file = os.path.join(app.root_path, "static", "dashboard_cache.json")
    if not os.path.exists(cache_file):
        return "Dashboard data not available. Please run /increment first."

    import json
    with open(cache_file) as f:
        data = json.load(f)

    return render_template("dashboard.html", **data)


@app.route("/reports")
def reports():
    report_list = os.listdir("static/reports/")
    return render_template(
        "report.html", report_list=report_list
    )



@app.route('/success', methods=['POST'])
def success():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    if request.method == 'POST':  
        f = request.files['file']
        if f:
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], f.filename)
            f.save(save_path)  
            return redirect(url_for('reports'))


@app.route("/reset_db")
def reset_db():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()

    reset_everything()
    compute_dashboard_data()

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

@app.route("/cache")
def compute_dashboard_data():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    all_dates = [d[0] for d in db.session.query(Holding.date).distinct().order_by(Holding.date).all()]
    if not all_dates:
        return

    current_holdings = get_current_holdings_longnames()
    past_holdings = get_past_holdings_longnames(current_holdings)

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
    omx_data = [x * 150000 / omx_data_temp.iloc[0] for x in omx_data_temp]

    gspc_data_temp = index_pivot.get('^GSPC', pd.Series()).reindex(series.index).fillna(method='ffill')
    gspc_data = [x * 150000 / gspc_data_temp.iloc[0] for x in gspc_data_temp]

    line_labels = df_series["date"].dt.strftime("%Y-%m-%d").tolist()
    line_data = df_series["value"].tolist()

    y_max = round(df_series["value"].max() * 1.05, -4)
    y_min = round(df_series["value"].min() * 0.95, -4)

    latest_cash_entry = Cash.query.filter_by(date=latest_date).first()
    cash_val = latest_cash_entry.balance if latest_cash_entry else 0

    dashboard_cache = {
        "latest_value": round(today_val, 2),
        "pct_changes": pct_changes,
        "line_labels": line_labels,
        "line_data": line_data,
        "omx_data": omx_data,
        "gspc_data": gspc_data,
        "y_max": y_max,
        "y_min": y_min,
        "cash": round(cash_val),
        "current": current_holdings,
        "past": past_holdings,
    }

    cache_file = os.path.join(app.root_path, "static", "dashboard_cache.json")
    with open(cache_file, "w") as f:
        json.dump(dashboard_cache, f)

    print("Dashboard cache updated.")
        
    return redirect(url_for('dashboard'))



def reset_everything():
    with app.app_context():
        db.drop_all()
        db.create_all()
        bootstrap_data()

import os
from apscheduler.schedulers.background import BackgroundScheduler




if __name__ == "__main__":    
    try:
        scheduler = BackgroundScheduler()

        scheduler.add_job(incremental_update, 'cron', hour=23, minute=0)

        scheduler.start()
        
        port = int(os.environ.get("PORT", 8080))
        app.run(host="0.0.0.0", port=port)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
