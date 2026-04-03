import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
import os
from pymongo import MongoClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "models", "isolation_forest.pkl")

def train_isolation_model():
    client = MongoClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017/"))
    db = client[os.getenv("MONGODB_DB_NAME", "digital_behaviour_twin")]
    twin_collection = db["behaviour_twin"]

    data = list(twin_collection.find({}, {"_id": 0}))

    if len(data) < 2:
       if len(data) < 2:
        print("Not enough data. Generating synthetic variation for training...")

    # Create synthetic variations from existing data
        synthetic = []
        for user in data:
            p = user.get("productive_time", 0)
            d = user.get("distracting_time", 0)

            synthetic.append([p, d])
            synthetic.append([p * 0.8, d * 1.2])
            synthetic.append([p * 1.2, d * 0.8])
            synthetic.append([p * 0.5, d * 1.5])

        X = np.array(synthetic)
    else:
        X = []
        for user in data:
            productive = user.get("productive_time", 0)
            distracting = user.get("distracting_time", 0)
            X.append([productive, distracting])
        X = np.array(X)

    X = []

    for user in data:
        productive = user.get("productive_time", 0)
        distracting = user.get("distracting_time", 0)
        X.append([productive, distracting])

    X = np.array(X)

    model = IsolationForest(contamination=0.1, random_state=42)
    model.fit(X)

    joblib.dump(model, MODEL_PATH)
    print("Isolation Forest trained on real user data!")

def load_isolation_model():
    return joblib.load(MODEL_PATH)

def predict_anomaly(productive, distracting):
    model = load_isolation_model()
    prediction = model.predict([[productive, distracting]])
    return prediction[0]  # -1 = anomaly, 1 = normal
