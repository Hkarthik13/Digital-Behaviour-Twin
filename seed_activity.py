from pymongo import MongoClient
from datetime import datetime, timedelta
import random

client = MongoClient("mongodb://localhost:27017/")
db = client["digital_behaviour_twin"]
collection = db["activity_logs"]

user_id = "test_user_1"

activities = ["productive", "distracting"]

data = []

for i in range(50):
    data.append({
        "user_id": user_id,
        "activity_type": random.choice(activities),
        "duration": random.randint(5, 60),
        "timestamp": datetime.now() - timedelta(hours=random.randint(1, 120))
    })

collection.insert_many(data)

print("✅ Dummy activity data inserted")
