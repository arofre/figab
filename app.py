import datetime
import os
import json
import threading
import numpy as np
import pandas as pd
from flask import Flask, render_template, request, Response, redirect, url_for, flash, send_from_directory
from flask_apscheduler import APScheduler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from collections import defaultdict
import json
import bisect
from portfolio_tracker.portfolio import Portfolio_tracker
from dateutil.relativedelta import relativedelta
import threading
from flask import jsonify

app = Flask(__name__)
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()
UPLOAD_FOLDER = os.path.join(app.root_path, 'static/reports')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.secret_key = os.environ.get("SECRET_KEY", "fallback-dev-key")

with app.app_context():
    portfolio_tracker = Portfolio_tracker(initial_cash=150000,currency="SEK", csv_file="transactions.csv")

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
    

def calculate_portfolio_value():
    return portfolio_tracker.get_portfolio_value(datetime.date.today())


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
    today_date = datetime.date.today()

    data = portfolio_tracker.get_portfolio_value(datetime.date(2025,2,17),today_date)
    value = list(data.values())

    line_labels = [ts.strftime("%Y-%m-%d") for ts in data]

    cash = portfolio_tracker.get_portfolio_cash(today_date)

    week_ago = data[today_date - relativedelta(weeks=1)]
    month_ago = data[today_date - relativedelta(months=1)]
    try:
        year_ago = data[today_date - relativedelta(years=1)]
    except:
        year_ago = value[0]
    pct_changes = {"This Week": (data[today_date] - week_ago) / week_ago * 100,
                    "This Month": (data[today_date] -  month_ago) / month_ago * 100,
                    "This Year": (data[today_date] - year_ago) / year_ago * 100,
                    "All Time": (value[-1] - value[0]) / value[0] * 100
                   }

    y_max = max(value) * 1.05
    y_min = min(value) * 0.95

    omx_data = list((np.array(portfolio_tracker.get_index_returns("^OMX", datetime.date(2025,2,17), today_date)) + 1 )* 150000)
    gspc_data = list((np.array(portfolio_tracker.get_index_returns("^GSPC", datetime.date(2025,2,17), today_date)) + 1) * 150000)
    current_holdings = portfolio_tracker.get_current_holdings()
    past_holdings = portfolio_tracker.get_past_holdings()

    cache_file = os.path.join(app.root_path, "static", "dashboard_cache.json")
    with open(cache_file, "w") as f:
        json.dump({
            "latest_value": round(value[-1], 2),
            "pct_changes": pct_changes,
            "line_labels": line_labels,
            "line_data": value,
            "omx_data": omx_data,
            "gspc_data": gspc_data,
            "y_max": y_max,
            "y_min": y_min,
            "cash": round(cash, 0),
            "current": current_holdings,
            "past": past_holdings,
            }, f)

    print("Dashboard cache updated.")

@app.route("/increment")
def incremental_update():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    run_incremental_update()

def run_incremental_update():
    portfolio_tracker.update_portfolio()
    compute_dashboard_data_internal()

@scheduler.task(
    "cron",
    id="daily_incremental_update",
    hour=23,
    minute=45,
    misfire_grace_time=300
)
def scheduled_incremental_update():
    run_incremental_update()

if __name__ == "__main__":    
    try:
        port = int(os.environ.get("PORT", 8080))
        app.run(host="0.0.0.0", port=port)
    except (KeyboardInterrupt, SystemExit):
        pass
