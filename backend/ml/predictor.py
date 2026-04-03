import os
import joblib
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(BASE_DIR, "models")

classification_model = joblib.load(os.path.join(MODEL_DIR, "focus_classification_model.pkl"))
regression_model = joblib.load(os.path.join(MODEL_DIR, "focus_regression_model.pkl"))
kmeans_model = joblib.load(os.path.join(MODEL_DIR, "kmeans_model.pkl"))

FEATURE_COLUMNS = [
    "total_productive_time",
    "total_distracting_time",
    "productive_ratio",
    "switch_frequency",
    "avg_session_duration"
]

def prepare_input(features: dict):
    return pd.DataFrame([features])[FEATURE_COLUMNS]

def predict_all(features: dict):
    try:
        input_df = prepare_input(features)

        cluster = int(kmeans_model.predict(input_df)[0])
        focus_level = classification_model.predict(input_df)[0]
        predicted_focus = float(regression_model.predict(input_df)[0])

        return {
            "cluster": cluster,
            "focus_level": focus_level,
            "predicted_focus_score": predicted_focus
        }

    except Exception as e:
        print("Prediction Error:", e)
        return None