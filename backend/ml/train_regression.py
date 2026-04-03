import os
import pandas as pd
import joblib
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.metrics import r2_score, mean_absolute_error

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
y = df["next_day_focus"]

X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42
)

pipeline = Pipeline([
    ("scaler", StandardScaler()),
    ("model", RandomForestRegressor(n_estimators=300, random_state=42))
])

pipeline.fit(X_train, y_train)

preds = pipeline.predict(X_test)

print("R2 Score:", r2_score(y_test, preds))
print("MAE:", mean_absolute_error(y_test, preds))

MODEL_DIR = os.path.join(BASE_DIR, "models")
os.makedirs(MODEL_DIR, exist_ok=True)

joblib.dump(pipeline, os.path.join(MODEL_DIR, "focus_regression_model.pkl"))

print("✅ Regression Model Saved")