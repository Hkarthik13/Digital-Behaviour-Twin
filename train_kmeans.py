import pandas as pd
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler
import joblib

#  Load dataset
df = pd.read_csv("../../dataset/behaviour_dataset.csv")

#  Select features for AI
features = df[
    [
        "total_productive_time",
        "total_distracting_time",
        "focus_score",
        "session_avg",
        "peak_productive_hour"
    ]
]

# 🧼 Handle missing values
features = features.fillna(0)

# 📏 Normalize features
scaler = StandardScaler()
scaled_features = scaler.fit_transform(features)

# 🧠 Train K-Means
kmeans = KMeans(n_clusters=3, random_state=42)
clusters = kmeans.fit_predict(scaled_features)

# 📌 Attach cluster labels
df["behaviour_cluster"] = clusters

# 💾 Save model + scaler
joblib.dump(kmeans, "kmeans_model.pkl")
joblib.dump(scaler, "scaler.pkl")

# 💾 Save clustered dataset
df.to_csv("../../dataset/behaviour_clustered.csv", index=False)

print("✅ K-Means Behaviour Twin model trained successfully!")
