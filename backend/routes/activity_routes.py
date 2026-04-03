from flask import Blueprint, jsonify
from database.db import db

activity_bp = Blueprint("activity", __name__)

@activity_bp.route("/activities/<email>", methods=["GET"])
def get_user_activities(email):

    activities = list(db.activities.find({"email": email}, {"_id": 0}))

    return jsonify(activities)