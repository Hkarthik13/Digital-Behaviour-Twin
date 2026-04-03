import os
import pandas as pd
import joblib
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.metrics import classification_report

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

# Create 3-level focus category from next_day_focus
df["focus_level"] = pd.qcut(
    df["next_day_focus"],
    q=3,
    labels=["low", "medium", "high"]
)

X = df[FEATURE_COLUMNS]
y = df["focus_level"]

X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42, stratify=y
)

pipeline = Pipeline([
    ("scaler", StandardScaler()),
    ("model", RandomForestClassifier(n_estimators=300, random_state=42))
])

pipeline.fit(X_train, y_train)

preds = pipeline.predict(X_test)
print(classification_report(y_test, preds))

MODEL_DIR = os.path.join(BASE_DIR, "models")
os.makedirs(MODEL_DIR, exist_ok=True)

joblib.dump(pipeline, os.path.join(MODEL_DIR, "focus_classification_model.pkl"))

print("✅ Classification Model Saved")