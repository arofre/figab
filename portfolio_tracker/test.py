from portfolio import Portfolio_tracker
import datetime
portfolio = Portfolio_tracker(initial_cash=150000, currency="SEK", csv_file="transactions.csv")
today_date = datetime.date.today()
