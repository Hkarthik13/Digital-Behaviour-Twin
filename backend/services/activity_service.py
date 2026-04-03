from database.db import db
from datetime import datetime

def log_activity(email, app, duration):

    activity = {
        "email": email,
        "app_name": app,
        "duration": duration,
        "timestamp": datetime.utcnow()
    }

    db.activities.insert_one(activity)

    return {"message": "Activity saved"}