# tasks.py
from datetime import datetime
from models import db, Holding, HistoricalPrice
from bootstrap import bootstrap_data
from update import update_close_prices
import os

def scheduled_update(app):
    with app.app_context():
        print(f"[{datetime.now()}] Running scheduled update...")
        transaction_file = "transactions.csv"
        latest_db_date = db.session.query(Holding.date).order_by(Holding.date.desc()).first()
        latest_db_date = latest_db_date[0] if latest_db_date else None

        if transaction_file and os.path.exists(transaction_file):
            file_mod_time = datetime.fromtimestamp(os.path.getmtime(transaction_file))

            if not latest_db_date or file_mod_time.date() > latest_db_date:
                print("Detected new transactions. Rebootstrapping data...")
                bootstrap_data()
            else:
                print("No new transactions detected.")

        tickers = db.session.query(HistoricalPrice.ticker).distinct().all()
        tickers = [t[0] for t in tickers]

        for ticker in tickers:
            update_close_prices(ticker)

        print("Scheduled update completed.")
