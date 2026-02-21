import datetime
import os
import re
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
from FinTrack import FinTrack, Config
from dateutil.relativedelta import relativedelta
import threading
from flask import jsonify
import sqlite3
import gc
app = Flask(__name__)
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()
UPLOAD_FOLDER = os.path.join(app.root_path, 'static/reports')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.secret_key = os.environ.get("SECRET_KEY", "fallback-dev-key")

REPORT_FOLDERS = ["Monthly reports", "Board meetings", "General meeting"]
CSV_FILE = "transactions.csv"

with app.app_context():
    portfolio_tracker = FinTrack(initial_cash=150000,currency="SEK", csv_file=CSV_FILE)

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


def sort_key_for_report(filename):
    name = os.path.splitext(filename)[0].lower()

    MONTHS = {
        'january': 1, 'jan': 1,
        'february': 2, 'feb': 2,
        'march': 3, 'mar': 3,
        'april': 4, 'apr': 4,
        'may': 5,
        'june': 6, 'jun': 6,
        'july': 7, 'jul': 7,
        'august': 8, 'aug': 8,
        'september': 9, 'sep': 9, 'sept': 9,
        'october': 10, 'oct': 10,
        'november': 11, 'nov': 11,
        'december': 12, 'dec': 12,
    }

    m = re.search(r'(\d{4})[-_./ ](\d{1,2})', name)
    if m:
        return (int(m.group(1)), int(m.group(2)))

    for word in sorted(MONTHS, key=len, reverse=True):
        if word in name:
            year_m = re.search(r'\d{4}', name)
            if year_m:
                return (int(year_m.group()), MONTHS[word])

    m = re.search(r'(\d{4})(\d{2})', name)
    if m:
        return (int(m.group(1)), int(m.group(2)))

    return (0, 0)


def get_reports_by_folder():
    base = app.config['UPLOAD_FOLDER']
    result = {}

    for folder in REPORT_FOLDERS:
        folder_path = os.path.join(base, folder)
        os.makedirs(folder_path, exist_ok=True)
        files = [
            f for f in os.listdir(folder_path)
            if os.path.isfile(os.path.join(folder_path, f)) and not f.startswith('.')
        ]
        files.sort(key=sort_key_for_report, reverse=True)
        result[folder] = files

    root_files = [
        f for f in os.listdir(base)
        if os.path.isfile(os.path.join(base, f)) and not f.startswith('.')
    ]
    root_files.sort(key=sort_key_for_report, reverse=True)
    if root_files:
        result['Other'] = root_files

    return result


def load_transactions():
    """Read transactions.csv and return list of dicts."""
    transactions = []
    if not os.path.exists(CSV_FILE):
        return transactions
    with open(CSV_FILE, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split(';')
        # Pad to 5 fields
        while len(parts) < 5:
            parts.append('')
        transactions.append({
            'Ticker': parts[0],
            'Date':   parts[1],
            'Type':   parts[2],
            'Amount': parts[3],
            'Price':  parts[4],
        })
    return transactions


def save_transactions(transactions):
    """Write list of dicts back to transactions.csv."""
    with open(CSV_FILE, 'w', encoding='utf-8') as f:
        for tx in transactions:
            price = tx.get('Price', '')
            f.write(f"{tx['Ticker']};{tx['Date']};{tx['Type']};{tx['Amount']};{price}\n")


@app.route("/delete_report", methods=["POST"])
def delete_report():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    filename = request.form.get("delete_file", "").strip()
    folder = request.form.get("delete_folder", "").strip()

    if not filename:
        flash("Please provide a filename.")
        return redirect(url_for("admin_dashboard"))

    if folder and folder in REPORT_FOLDERS:
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], folder, filename)
    else:
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)

    if os.path.exists(file_path):
        os.remove(file_path)
        flash(f"File '{filename}' deleted successfully.")
    else:
        flash(f"File '{filename}' does not exist.")
    
    return redirect(url_for("admin_dashboard"))


@app.route("/reports/<path:filename>", endpoint="custom_reports")
def reports_file(filename):
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
    reports_data = get_reports_by_folder()
    return render_template("report.html", reports_data=reports_data, report_folders=REPORT_FOLDERS)


@app.route('/success', methods=['POST'])
def success():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    if request.method == 'POST':
        f = request.files.get('file')
        folder = request.form.get('folder', '').strip()

        if f:
            if folder and folder in REPORT_FOLDERS:
                save_dir = os.path.join(app.config['UPLOAD_FOLDER'], folder)
            else:
                save_dir = app.config['UPLOAD_FOLDER']

            os.makedirs(save_dir, exist_ok=True)
            save_path = os.path.join(save_dir, f.filename)
            f.save(save_path)
            flash(f"Report '{f.filename}' uploaded successfully.")
            return redirect(url_for('admin_dashboard'))


@app.route("/admin", methods=["GET"])
def admin_dashboard():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()

    transactions = load_transactions()
    return render_template("admin.html", report_folders=REPORT_FOLDERS, transactions=transactions)


@app.route("/admin/add_transaction", methods=["POST"])
def add_transaction():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()

    ticker   = request.form.get("ticker", "").strip().upper()
    amount   = request.form.get("amount", "").strip()
    action   = request.form.get("action", "").strip()
    date_str = request.form.get("date", "").strip()
    price    = request.form.get("price", "").strip()

    if not ticker or not amount or action not in ("Buy", "Sell", "Short"):
        flash("Please fill out all required fields correctly.")
        return redirect(url_for("admin_dashboard"))

    try:
        int(amount)
    except ValueError:
        flash("Amount must be an integer.")
        return redirect(url_for("admin_dashboard"))

    line = f"{ticker};{date_str};{action};{amount};{price}\n"

    with open(CSV_FILE, "a", encoding='utf-8') as f:
        f.write(line)

    flash(f"Transaction recorded: {action} {amount} × {ticker} on {date_str}.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete_transaction", methods=["POST"])
def delete_transaction():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()

    try:
        row_index = int(request.form.get("row_index", -1))
    except ValueError:
        flash("Invalid row index.")
        return redirect(url_for("admin_dashboard"))

    transactions = load_transactions()

    if row_index < 0 or row_index >= len(transactions):
        flash("Transaction not found.")
        return redirect(url_for("admin_dashboard"))

    removed = transactions.pop(row_index)
    save_transactions(transactions)

    flash(f"Deleted: {removed['Type']} {removed['Amount']} × {removed['Ticker']} on {removed['Date']}.")
    return redirect(url_for("admin_dashboard"))


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
    pct_changes = {"Last Week": (data[today_date] - week_ago) / week_ago * 100,
                    "Last Month": (data[today_date] -  month_ago) / month_ago * 100,
                    "Last year": (data[today_date] - year_ago) / year_ago * 100,
                    "All Time": (value[-1] - value[0]) / value[0] * 100
                   }

    y_max = max(value) * 1.05
    y_min = min(value) * 0.95

    omx_data = list((np.array(portfolio_tracker.get_index_returns("^OMX", datetime.date(2025,2,17), today_date)) + 1 )* 150000)
    gspc_data = list((np.array(portfolio_tracker.get_index_returns("^GSPC", datetime.date(2025,2,17), today_date)) + 1) * 150000)
    current_holdings = portfolio_tracker.get_current_holdings()
    past_holdings = portfolio_tracker.get_past_holdings()

    diff_omx = len(value) - len(omx_data)
    diff_gspc = len(value) - len(gspc_data)

    for _ in range(diff_omx):
        omx_data.append(omx_data[-1])
    for _ in range(diff_gspc):
        gspc_data.append(gspc_data[-1])

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


@app.route("/reset_db")
def reset_database(user_id=None):
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    db_path = Config.get_db_path(user_id)

    print(f"Database location: {db_path}")

    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            conn.close()
        except Exception:
            pass
        gc.collect()

        try:
            os.remove(db_path)
            print("Next time you initialize a portfolio, a fresh database will be created.")
        except PermissionError as e:
            print(f"Could not delete database: {e}")
            return "Database is still in use. Try again in a moment.", 500
    else:
        print("Database file not found. Nothing to delete.")

    return "Database reset successfully.", 200

@app.route("/returns")
def returns():
    return portfolio_tracker.print_stock_returns(
                from_date=datetime.date(2026, 1, 23),
                to_date=datetime.date.today()
                ).replace('\n', '<br>')


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
