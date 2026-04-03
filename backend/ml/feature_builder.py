def build_features(email, activities_collection):
    logs = list(
        activities_collection.find({"email": email}).sort("timestamp", 1)
    )

    productive = 0
    distracting = 0

    for log in logs:
        if log["type"] == "productive":
            productive += log["duration"]
        elif log["type"] == "distracting":
            distracting += log["duration"]

    total = productive + distracting
    productive_ratio = productive / total if total > 0 else 0

    # Switch frequency
    switch_count = 0
    for i in range(1, len(logs)):
        if logs[i]["app"] != logs[i - 1]["app"]:
            switch_count += 1

    switch_frequency = switch_count

    # Average session duration
    if len(logs) > 0:
        total_duration = sum(log["duration"] for log in logs)
        avg_session_duration = total_duration / len(logs)
    else:
        avg_session_duration = 0

    return {
        "total_productive_time": productive,
        "total_distracting_time": distracting,
        "productive_ratio": productive_ratio,
        "switch_frequency": switch_frequency,
        "avg_session_duration": avg_session_duration
    }