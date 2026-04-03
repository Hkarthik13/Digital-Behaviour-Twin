import os
import pandas as pd
import joblib
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE_DIR, "..", "..", "dataset", "behaviour_dataset.csv")

df = pd.read_csv(DATA_PATH)

FEATURE_COLUMNS = [
    "total_productive_time",
    "total_distracting_time",
    "productive_ratio",
    "switch_frequency",
    "avg_session_duration"
]

X = df[FEATURE_COLUMNS]

pipeline = Pipeline([
    ("scaler", StandardScaler()),
    ("kmeans", KMeans(n_clusters=3, random_state=42))
])

pipeline.fit(X)

MODEL_DIR = os.path.join(BASE_DIR, "models")
os.makedirs(MODEL_DIR, exist_ok=True)

joblib.dump(pipeline, os.path.join(MODEL_DIR, "kmeans_model.pkl"))

print("✅ KMeans Model Saved")