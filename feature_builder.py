import pandas as pd
from pymongo import MongoClient
from datetime import datetime

# 🔗 MongoDB connection
client = MongoClient("mongodb://localhost:27017/")
db = client["digital_behaviour_twin"]
collection = db["activity_logs"]

# 📥 Load data from MongoDB
data = list(collection.find({}, {"_id": 0}))

if len(data) == 0:
    print("No activity data found.")
    exit()

df = pd.DataFrame(data)

# ⏰ Convert timestamp
df["timestamp"] = pd.to_datetime(df["timestamp"])
df["date"] = df["timestamp"].dt.date
df["hour"] = df["timestamp"].dt.hour
df["day_of_week"] = df["timestamp"].dt.day_name()

# 📊 Aggregate features (per user per day)
features = []

grouped = df.groupby(["user_id", "date"])

for (user, date), group in grouped:
    productive = group[group["activity_type"] == "productive"]
    distracting = group[group["activity_type"] == "distracting"]

    total_productive = productive["duration"].sum()
    total_distracting = distracting["duration"].sum()

    total_time = total_productive + total_distracting
    focus_score = total_productive / total_time if total_time > 0 else 0

    peak_hour = (
        productive.groupby("hour")["duration"].sum().idxmax()
        if not productive.empty else -1
    )

    session_avg = group["duration"].mean()

    features.append({
        "user_id": user,
        "date": date,
        "day_of_week": group["day_of_week"].iloc[0],
        "total_productive_time": total_productive,
        "total_distracting_time": total_distracting,
        "focus_score": round(focus_score, 2),
        "peak_productive_hour": peak_hour,
        "session_avg": round(session_avg, 2)
    })

# 📁 Save dataset
feature_df = pd.DataFrame(features)
feature_df.to_csv("../../dataset/behaviour_dataset.csv", index=False)

print("✅ Feature dataset generated successfully!")
