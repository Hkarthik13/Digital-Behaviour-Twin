import joblib
import numpy as np
import os

# 📂 Get current file directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 🔄 Load trained model & scaler safely
kmeans = joblib.load(os.path.join(BASE_DIR, "kmeans_model.pkl"))
scaler = joblib.load(os.path.join(BASE_DIR, "scaler.pkl"))

def predict_behaviour(feature_dict):
    feature_vector = np.array([
        feature_dict["total_productive_time"],
        feature_dict["total_distracting_time"],
        feature_dict["focus_score"],
        feature_dict["session_avg"],
        feature_dict["peak_productive_hour"]
    ]).reshape(1, -1)

    scaled_vector = scaler.transform(feature_vector)
    cluster = kmeans.predict(scaled_vector)[0]

    return int(cluster)
