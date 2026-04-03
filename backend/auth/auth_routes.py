from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token
from database.db import users_collection
from auth.auth_utils import hash_password, check_password

auth_bp = Blueprint("auth", __name__)

@auth_bp.route("/register", methods=["POST"])
def register():
    data = request.json
    users_collection.insert_one({
        "email": data["email"],
        "password": hash_password(data["password"])
    })
    return jsonify({"msg": "User registered successfully"})

@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.json
    user = users_collection.find_one({"email": data["email"]})
    if user and check_password(user["password"], data["password"]):
        token = create_access_token(identity=data["email"])
        return jsonify(access_token=token)
    return jsonify({"msg": "Invalid credentials"}), 401
