from collections import defaultdict

def get_best_focus_hours(email, activities):

    logs = list(activities.find({"email": email}))

    hourly_data = defaultdict(lambda: {"productive":0, "total":0})

    for log in logs:

        hour = log["timestamp"].hour

        hourly_data[hour]["total"] += 1

        if log["type"] == "productive":
            hourly_data[hour]["productive"] += 1

    focus_scores = []

    for hour,data in hourly_data.items():

        if data["total"] == 0:
            continue

        score = data["productive"] / data["total"]

        focus_scores.append({
            "hour": hour,
            "score": round(score,2)
        })

    focus_scores.sort(key=lambda x:x["score"], reverse=True)

    return focus_scores[:5]