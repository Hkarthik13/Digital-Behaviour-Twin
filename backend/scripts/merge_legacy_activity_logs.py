import argparse
from datetime import datetime
from typing import Any

from pymongo import MongoClient


def normalize_activity_type(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"productive", "distracting", "neutral"}:
        return raw
    return "neutral"


def build_target_doc(source_doc: dict[str, Any], target_email: str, default_app: str) -> dict[str, Any]:
    return {
        "email": target_email,
        "app": source_doc.get("app") or default_app,
        "duration": int(source_doc.get("duration") or 0),
        "timestamp": source_doc.get("timestamp"),
        "type": normalize_activity_type(source_doc.get("activity_type")),
        "imported_from": "activity_logs",
        "legacy_id": str(source_doc["_id"]),
        "legacy_user_id": source_doc.get("user_id"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge legacy activity_logs documents into the current activities collection."
    )
    parser.add_argument("--mongo-uri", default="mongodb://localhost:27017/")
    parser.add_argument("--source-mongo-uri", help="Optional source Mongo URI. Defaults to --mongo-uri.")
    parser.add_argument("--target-mongo-uri", help="Optional target Mongo URI. Defaults to --mongo-uri.")
    parser.add_argument("--db-name", default="digital_behaviour_twin")
    parser.add_argument("--source-db-name", help="Optional source DB name. Defaults to --db-name.")
    parser.add_argument("--target-db-name", help="Optional target DB name. Defaults to --db-name.")
    parser.add_argument("--source-collection", default="activity_logs")
    parser.add_argument("--target-collection", default="activities")
    parser.add_argument("--source-user-id", help="Optional legacy user_id filter.")
    parser.add_argument("--target-email", required=True, help="Destination email in the current schema.")
    parser.add_argument(
        "--default-app",
        default="Imported legacy activity",
        help="Fallback app name when the legacy document has no app field.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write missing documents. Without this flag the script only shows a dry run.",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild behaviour_twin, risk_scores, and ml_states after merge.",
    )
    args = parser.parse_args()

    source_client = MongoClient(args.source_mongo_uri or args.mongo_uri)
    target_client = MongoClient(args.target_mongo_uri or args.mongo_uri)
    source_db = source_client[args.source_db_name or args.db_name]
    target_db = target_client[args.target_db_name or args.db_name]
    source_collection = source_db[args.source_collection]
    target_collection = target_db[args.target_collection]

    query: dict[str, Any] = {}
    if args.source_user_id:
        query["user_id"] = args.source_user_id

    source_docs = list(source_collection.find(query))
    source_ids = [str(doc["_id"]) for doc in source_docs]

    existing_legacy_ids = set()
    if source_ids:
        existing_legacy_ids = {
            doc["legacy_id"]
            for doc in target_collection.find(
                {
                    "email": args.target_email,
                    "imported_from": args.source_collection,
                    "legacy_id": {"$in": source_ids},
                },
                {"legacy_id": 1},
            )
        }

    pending_docs = [
        build_target_doc(doc, args.target_email, args.default_app)
        for doc in source_docs
        if str(doc["_id"]) not in existing_legacy_ids
    ]

    print(f"Source collection    : {args.source_collection}")
    print(f"Target collection    : {args.target_collection}")
    print(f"Target email         : {args.target_email}")
    print(f"Source docs matched  : {len(source_docs)}")
    print(f"Already imported     : {len(existing_legacy_ids)}")
    print(f"Pending merge        : {len(pending_docs)}")

    if pending_docs:
        first_doc = pending_docs[0]
        print("Sample transformed doc:")
        print(
            {
                "email": first_doc["email"],
                "app": first_doc["app"],
                "duration": first_doc["duration"],
                "timestamp": first_doc["timestamp"],
                "type": first_doc["type"],
                "legacy_user_id": first_doc["legacy_user_id"],
            }
        )

    if not args.apply:
        print("Dry run only. Re-run with --apply to insert pending documents.")
        return

    if not pending_docs:
        print("Nothing to insert.")
        return

    result = target_collection.insert_many(pending_docs, ordered=False)
    print(f"Inserted documents   : {len(result.inserted_ids)}")

    if args.rebuild:
        all_logs = list(target_collection.find({"email": args.target_email}, {"_id": 0, "type": 1, "duration": 1}))
        productive = sum(max(int(log.get("duration") or 0), 0) for log in all_logs if log.get("type") == "productive")
        distracting = sum(max(int(log.get("duration") or 0), 0) for log in all_logs if log.get("type") == "distracting")
        total = productive + distracting
        focus_score = round((productive / total) * 100) if total > 0 else 0
        risk = round((distracting / total) * 100) if total > 0 else 0
        now = datetime.now()

        target_db["behaviour_twin"].update_one(
            {"email": args.target_email},
            {"$set": {
                "email": args.target_email,
                "productive_time": productive,
                "distracting_time": distracting,
                "last_updated": now,
            }},
            upsert=True,
        )
        target_db["risk_scores"].update_one(
            {"email": args.target_email},
            {"$set": {
                "email": args.target_email,
                "risk_score": risk,
                "last_updated": now,
            }},
            upsert=True,
        )
        target_db["ml_states"].update_one(
            {"email": args.target_email},
            {"$set": {
                "email": args.target_email,
                "focus_level": "Highly Productive" if focus_score >= 75 else "Balanced" if focus_score >= 45 else "Highly Distracted",
                "predicted_score": focus_score,
                "predicted_focus_score": focus_score,
                "last_updated": now,
            }},
            upsert=True,
        )
        print("Rebuilt aggregate collections after merge.")


if __name__ == "__main__":
    main()
