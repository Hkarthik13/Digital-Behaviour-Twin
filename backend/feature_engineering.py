from datetime import datetime, timedelta
import numpy as np

def extract_features(activity_logs):

    total_productive = 0
    total_distracting = 0
    total_duration = 0
    session_count = 0

    last_app = None
    switch_frequency = 0

    for log in activity_logs:
        duration = log.get("duration", 0)
        category = log.get("type", "productive")

        total_duration += duration
        session_count += 1

        if category == "productive":
            total_productive += duration
        elif category == "distracting":
            total_distracting += duration

        # app switching logic
        current_app = log.get("app")
        if last_app and last_app != current_app:
            switch_frequency += 1

        last_app = current_app

    productive_ratio = (
        total_productive / total_duration if total_duration > 0 else 0
    )

    avg_session_duration = (
        total_duration / session_count if session_count > 0 else 0
    )

  
    return {
        "total_productive_time": total_productive,
        "total_distracting_time": total_distracting,
        "productive_ratio": productive_ratio,
        "switch_frequency": switch_frequency,
        "avg_session_duration": avg_session_duration
    }