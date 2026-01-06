from app import incremental_update
from apscheduler.schedulers.blocking import BlockingScheduler
import datetime

scheduler = BlockingScheduler()

scheduler.add_job(
    incremental_update,
    'date',
    run_date=datetime.datetime.now() + datetime.timedelta(seconds=10)
)

scheduler.start()
