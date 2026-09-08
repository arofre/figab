import datetime
import os
import re
import json
import threading
import numpy as np
from flask import Flask, render_template, request, Response, redirect, url_for, flash, send_from_directory
from flask_apscheduler import APScheduler
from FinTrack import FinTrack, Config
from dateutil.relativedelta import relativedelta
import sqlite3
import gc
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

app = Flask(__name__)
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()
UPLOAD_FOLDER = os.path.join(app.root_path, 'static/reports')
ALUMNI_IMAGE_FOLDER = os.path.join(app.root_path, 'static', 'alumni')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['ALUMNI_IMAGE_FOLDER'] = ALUMNI_IMAGE_FOLDER

app.secret_key = os.environ["FLASK_SECRET_KEY"]

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH")

ph = PasswordHasher()

REPORT_FOLDERS = ["Monthly reports", "Board meetings", "General meeting"]
CSV_FILE = "transactions.csv"
tracker_lock = threading.RLock()


def create_portfolio_tracker():
    return FinTrack(initial_cash=150000, currency="SEK", csv_file=CSV_FILE)


with app.app_context():
    portfolio_tracker = create_portfolio_tracker()

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
    with tracker_lock:
        return portfolio_tracker.get_portfolio_value(datetime.date.today())


def percent_change(series, start_date, today_val):
    s = series[series.index <= start_date]
    if s.empty:
        return None
    return ((today_val - s.iloc[-1]) / s.iloc[-1]) * 100


def check_auth(username, password):
    if username != ADMIN_USERNAME:
        return False

    if not ADMIN_PASSWORD_HASH:
        return False

    try:
        return ph.verify(ADMIN_PASSWORD_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False

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


def get_alumni_images():
    folder = app.config['ALUMNI_IMAGE_FOLDER']
    os.makedirs(folder, exist_ok=True)

    allowed_ext = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'}
    files = [
        f for f in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, f))
        and os.path.splitext(f)[1].lower() in allowed_ext
        and not f.startswith('.')
    ]
    files.sort(key=lambda name: name.lower())

    return [url_for('static', filename=f'alumni/{filename}') for filename in files]


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


@app.route("/alumni")
def alumni():
    image_urls = get_alumni_images()
    return render_template("alumni.html", image_urls=image_urls)


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
    flash("No file uploaded.")
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
    try:
        compute_dashboard_data_internal()
    except Exception:
        app.logger.exception("Dashboard cache update failed")
        return "Could not update dashboard cache.", 500
    return redirect(url_for("dashboard"))


def compute_dashboard_data_internal():
    today_date = datetime.date.today()
    with tracker_lock:
        data = portfolio_tracker.get_portfolio_value(datetime.date(2025, 2, 17), today_date)
        current_holdings = portfolio_tracker.get_current_holdings()
        past_holdings = portfolio_tracker.get_past_holdings()
        omx_returns = portfolio_tracker.get_index_returns("^OMX", datetime.date(2025, 2, 17), today_date)
        gspc_returns = portfolio_tracker.get_index_returns("^GSPC", datetime.date(2025, 2, 17), today_date)

    if not data:
        raise ValueError("No portfolio data available.")

    points = sorted(data.items())
    dates = [date for date, _ in points]
    raw_values = [val for _, val in points]
    value = [v / 150000 for v in raw_values]
    line_labels = [ts.strftime("%Y-%m-%d") for ts in dates]
    latest_value = raw_values[-1]

    def value_on_or_before(target_date):
        for dt, val in reversed(points):
            if dt <= target_date:
                return val
        return None

    week_ago = value_on_or_before(today_date - relativedelta(weeks=1))
    month_ago = value_on_or_before(today_date - relativedelta(months=1))
    year_ago = value_on_or_before(today_date - relativedelta(years=1))

    def pct_from(reference):
        if reference in (None, 0):
            return None
        return (latest_value - reference) / reference * 100

    pct_changes = {
        "Last Week": pct_from(week_ago),
        "Last Month": pct_from(month_ago),
        "Last year": pct_from(year_ago),
        "All Time": (value[-1] - value[0]) / value[0] * 100 if len(value) > 1 and value[0] != 0 else None
    }

    y_max = max(value) * 1.05
    y_min = min(value) * 0.95

    omx_data = list(np.array(omx_returns) + 1) if omx_returns else []
    gspc_data = list(np.array(gspc_returns) + 1) if gspc_returns else []

    if len(value) > 0:
        if not omx_data:
            omx_data = [1.0] * len(value)
        if not gspc_data:
            gspc_data = [1.0] * len(value)

    if len(omx_data) < len(value):
        omx_data.extend([omx_data[-1]] * (len(value) - len(omx_data)))
    if len(gspc_data) < len(value):
        gspc_data.extend([gspc_data[-1]] * (len(value) - len(gspc_data)))
    if len(omx_data) > len(value):
        omx_data = omx_data[:len(value)]
    if len(gspc_data) > len(value):
        gspc_data = gspc_data[:len(value)]

    sharpe = sharpe_ratio(value)
    alpha = np.nan

    if len(value) > 1 and len(omx_data) > 1:
        portfolio_returns = np.diff(value) / np.array(value[:-1])
        benchmark_returns = np.diff(omx_data) / np.array(omx_data[:-1])
        min_len = min(len(portfolio_returns), len(benchmark_returns))
        if min_len >= 2:
            portfolio_returns = portfolio_returns[:min_len]
            benchmark_returns = benchmark_returns[:min_len]
            benchmark_variance = np.var(benchmark_returns)
            if benchmark_variance > 0:
                beta = np.cov(portfolio_returns, benchmark_returns)[0, 1] / benchmark_variance
                alpha = (np.mean(portfolio_returns) - beta * np.mean(benchmark_returns)) * 252 * 100

    cache_file = os.path.join(app.root_path, "static", "dashboard_cache.json")
    with open(cache_file, "w") as f:
        json.dump({
            "pct_changes": pct_changes,
            "line_labels": line_labels,
            "line_data": value,
            "omx_data": omx_data,
            "gspc_data": gspc_data,
            "y_max": y_max,
            "y_min": y_min,
            "current": current_holdings,
            "past": past_holdings,
            "sharpe": sharpe,
            "alpha": alpha,
            }, f)

    print("Dashboard cache updated.")


@app.route("/reset_db")
def reset_database(user_id=None):
    global portfolio_tracker
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    with tracker_lock:
        db_path = Config.get_db_path(user_id)
        print(f"Database location: {db_path}")

        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                conn.close()
            except sqlite3.Error:
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

        portfolio_tracker = create_portfolio_tracker()

    return "Database reset successfully.", 200

@app.route("/returns")
def returns():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()

    from_str = request.args.get("from")
    to_str = request.args.get("to")

    try:
        from_date = datetime.datetime.strptime(from_str, "%Y-%m-%d").date() if from_str else datetime.date(2026, 2, 18)
        to_date = datetime.datetime.strptime(to_str, "%Y-%m-%d").date() if to_str else datetime.date.today()
    except ValueError:
        return "Invalid date format. Use YYYY-MM-DD."

    with tracker_lock:
        return portfolio_tracker.print_stock_returns(
            from_date=from_date,
            to_date=to_date
        ).replace('\n', '<br>')

@app.route("/increment")
def incremental_update():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    try:
        run_incremental_update()
    except Exception:
        app.logger.exception("Incremental update failed")
        return "Incremental update failed.", 500
    return redirect(url_for("dashboard"))

def run_incremental_update():
    with tracker_lock:
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
        run_incremental_update()

        port = int(os.environ.get("PORT", 8080))
        app.run(host="0.0.0.0", port=port)
    except (KeyboardInterrupt, SystemExit):
        pass
