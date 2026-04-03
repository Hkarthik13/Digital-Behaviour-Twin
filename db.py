from pymongo import MongoClient

client = MongoClient("mongodb://localhost:27017/")
db = client["digital_behaviour_twin"]
users_collection = db["users"]