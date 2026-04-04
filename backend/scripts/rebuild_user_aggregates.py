import argparse
from datetime import datetime, timedelta

from pymongo import MongoClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild user aggregate documents from activities.")
    parser.add_argument("--mongo-uri", default="mongodb://localhost:27017/")
    parser.add_argument("--db-name", default="digital_behaviour_twin")
    parser.add_argument("--email", required=True)
    args = parser.parse_args()

    client = MongoClient(args.mongo_uri)
    db = client[args.db_name]

    activities = db["activities"]
    twin = db["behaviour_twin"]
    risk_scores = db["risk_scores"]
    ml_states = db["ml_states"]
    alerts = db["alerts"]

    logs = list(activities.find({"email": args.email}, {"_id": 0, "type": 1, "duration": 1, "timestamp": 1}))
    productive = sum(max(int(log.get("duration") or 0), 0) for log in logs if log.get("type") == "productive")
    distracting = sum(max(int(log.get("duration") or 0), 0) for log in logs if log.get("type") == "distracting")
    total = productive + distracting

    twin.update_one(
        {"email": args.email},
        {
            "$set": {
                "email": args.email,
                "productive_time": productive,
                "distracting_time": distracting,
                "last_updated": datetime.now(),
            }
        },
        upsert=True,
    )

    risk = round((distracting / total) * 100) if total > 0 else 0
    last_24h = datetime.now() - timedelta(hours=24)
    alert_count = alerts.count_documents({"email": args.email, "timestamp": {"$gte": last_24h}})
    risk = min(risk + min(alert_count * 2, 15), 100)

    risk_scores.update_one(
        {"email": args.email},
        {
            "$set": {
                "email": args.email,
                "risk_score": risk,
                "last_updated": datetime.now(),
            }
        },
        upsert=True,
    )

    focus_score = round((productive / total) * 100) if total > 0 else 0
    focus_level = "Highly Productive" if focus_score >= 75 else "Balanced" if focus_score >= 45 else "Highly Distracted"
    ml_states.update_one(
        {"email": args.email},
        {
            "$set": {
                "email": args.email,
                "focus_level": focus_level,
                "predicted_score": focus_score,
                "predicted_focus_score": focus_score,
                "last_updated": datetime.now(),
            }
        },
        upsert=True,
    )

    print(f"Rebuilt aggregates for {args.email}")
    print(f"Productive seconds   : {productive}")
    print(f"Distracting seconds  : {distracting}")
    print(f"Focus score          : {focus_score}")
    print(f"Risk score           : {risk}")


if __name__ == "__main__":
    main()
