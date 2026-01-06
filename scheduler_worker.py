from app import incremental_update
from apscheduler.schedulers.blocking import BlockingScheduler

scheduler = BlockingScheduler()
scheduler.add_job(incremental_update, 'cron', hour=23, minute=0)
scheduler.start()
