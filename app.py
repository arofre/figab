from datetime import datetime, timedelta, date
import os
import json
import threading
import numpy as np
import pandas as pd
from flask import Flask, render_template, request, Response, redirect, url_for, flash, send_from_directory
from sqlalchemy import func, select
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from collections import defaultdict
import json
import bisect
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

    rows = (
        db.session.query(HistoricalPrice)
        .filter(HistoricalPrice.ticker.in_(tickers))
        .filter(HistoricalPrice.date <= date)
        .order_by(HistoricalPrice.ticker, HistoricalPrice.date.desc())
        .all()
    )

    out = {}
    for row in rows:
        if row.ticker not in out:
            out[row.ticker] = row.close   
    return out

import threading
from flask import jsonify

@app.route("/increment")
def incremental_update():
    """
    Run the portfolio increment in a background thread to avoid web request timeouts.
    Returns immediately to the user.
    """

    def background_increment():
        """The actual heavy computation."""
        from sqlalchemy import func
        with app.app_context():
            # Determine start date
            last_price_date = db.session.query(func.max(HistoricalPrice.date)).scalar()
            start_date = (last_price_date + timedelta(days=1)) if last_price_date else DEFAULT_START_DATE

            # Fetch distinct tickers
            tickers = [t[0] for t in db.session.query(HistoricalPrice.ticker).distinct().all()]

            # Update prices for each ticker
            for ticker in tickers:
                try:
                    update_close_prices(ticker, start_date=start_date)
                except Exception as e:
                    print(f"Failed to update {ticker}: {e}")

            # Update holdings
            last_holding_date = db.session.query(func.max(Holding.date)).scalar()
            txs, _ = load_transactions("transactions.csv")
            from_date = (last_holding_date + timedelta(days=1)) if last_holding_date else DEFAULT_START_DATE
            to_date = date.today()
            starting_cash = (
                db.session.query(Cash.balance).filter(Cash.date == last_holding_date).scalar()
                if last_holding_date else STARTING_CASH
            )

            generate_holdings(
                transactions=txs,
                from_date=from_date,
                to_date=to_date,
                starting_cash=starting_cash
            )

            # Rebuild dashboard cache
            compute_dashboard_data_internal()

        print("Incremental update finished.")

    # Start background thread
    thread = threading.Thread(target=background_increment, daemon=True)
    thread.start()

    # Return immediately
    return jsonify({"status": "Incremental update started in background"}), 202




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
    compute_dashboard_data_internal()

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
    compute_dashboard_data_internal()
    return redirect(url_for("dashboard"))

def compute_dashboard_data_internal():
    """Fastest possible dashboard rebuild. Only O(n) operations."""


    # ---------------------------------------
    # 1) BULK LOAD ALL TABLES IN MEMORY
    # ---------------------------------------
    holdings = Holding.query.all()
    dividends = Dividend.query.all()
    cash_entries = Cash.query.all()
    prices = HistoricalPrice.query.all()

    if not holdings:
        return redirect(url_for("dashboard"))

    # ---------------------------------------
    # 2) FAST STRUCTURES
    # ---------------------------------------
    holdings_by_date = defaultdict(list)
    for h in holdings:
        holdings_by_date[h.date].append(h)

    dividends_by_date = defaultdict(list)
    for d in dividends:
        dividends_by_date[d.date].append(d)

    cash_by_date = {c.date: c.balance for c in cash_entries}
    

    # Price → ticker → sorted list of (date, close)
    price_lookup = defaultdict(list)
    for p in prices:
        price_lookup[p.ticker].append((p.date, p.close))

    for t in price_lookup:
        price_lookup[t].sort()

    def get_price(ticker, dt):
        """SQLite-safe binary search for most recent price before date."""
        lst = price_lookup.get(ticker)
        if not lst:
            return None
        idx = bisect.bisect_right(lst, (dt, 10**12)) - 1
        if idx >= 0:
            return lst[idx][1]
        return None

    all_dates = sorted(holdings_by_date.keys())
    if not all_dates:
        return redirect(url_for("dashboard"))

    # ---------------------------------------
    # 3) BUILD PORTFOLIO TIME SERIES
    # ---------------------------------------
    portfolio_values = {}

    for dt in all_dates:
        total = cash_by_date.get(dt, 0)

        # dividends
        for div in dividends_by_date.get(dt, []):
            # get shares at this date
            shares = 0
            for h in holdings_by_date[dt]:
                if h.ticker == div.ticker:
                    shares = h.shares
                    break
            total += div.amount * shares

        # holdings
        for h in holdings_by_date[dt]:
            price = get_price(h.ticker, dt)
            if price:
                total += h.shares * price

        portfolio_values[dt] = total

    # Convert to pandas for metrics
    series = pd.Series(portfolio_values)
    series.index = pd.to_datetime(series.index)

    latest_date = series.index[-1].date()
    first_date = series.index[0]
    today_val = series.iloc[-1]

    now = pd.Timestamp.now().normalize()

    def pct_change(start_dt):
        """Percent change relative to the latest value."""
        try:
            past = series.loc[:start_dt].iloc[-1]
            return (today_val - past) / past * 100
        except:
            return None

    pct_changes = {
        "This Week": pct_change(now - timedelta(days=7)),
        "This Month": pct_change(now.replace(day=1)),
        "This Year": pct_change(now.replace(month=1, day=1)),
        "All Time": pct_change(first_date)
    }

    df_series = series.reset_index()
    df_series.columns = ["date", "value"]

    line_labels = df_series["date"].dt.strftime("%Y-%m-%d").tolist()
    line_data = df_series["value"].tolist()

    y_max = round(max(line_data) * 1.05, -4)
    y_min = round(min(line_data) * 0.95, -4)

    # holdings lists
    current_holdings = list({h.longname for h in holdings_by_date[latest_date] if h.longname})
    all_longnames = {h.longname for h in holdings}
    past_holdings = list(all_longnames - set(current_holdings))


    index_tickers = ['^OMX', '^GSPC']
    index_prices_df = get_index_prices(index_tickers, series.index)
    index_pivot = index_prices_df.pivot(index='date', columns='ticker', values='close')

    # OMX
    omx_temp = index_pivot.get('^OMX', pd.Series()).reindex(series.index).fillna(method='ffill')
    omx_data = [x * 150_000 / omx_temp.iloc[0] for x in omx_temp]

    # GSPC
    gspc_temp = index_pivot.get('^GSPC', pd.Series()).reindex(series.index).fillna(method='ffill')
    gspc_data = [x * 150_000 / gspc_temp.iloc[0] for x in gspc_temp]


    # ---------------------------------------
    # 4) WRITE DASHBOARD CACHE FILE
    # ---------------------------------------
    cache_file = os.path.join(app.root_path, "static", "dashboard_cache.json")
    with open(cache_file, "w") as f:
        json.dump({
            "latest_value": round(today_val, 2),
            "pct_changes": pct_changes,
            "line_labels": line_labels,
            "line_data": line_data,
            "omx_data": omx_data,
            "gspc_data": gspc_data,
            "y_max": y_max,
            "y_min": y_min,
            "cash": round(cash_by_date.get(latest_date, 0)),
            "current": current_holdings,
            "past": past_holdings,
        }, f)

    print("Dashboard cache updated.")


def reset_everything():
    with app.app_context():
        db.drop_all()
        db.create_all()
        bootstrap_data()

import os




if __name__ == "__main__":    
    try:
        port = int(os.environ.get("PORT", 8080))
        app.run(host="0.0.0.0", port=port)
    except (KeyboardInterrupt, SystemExit):
        pass
