import os
import pandas as pd
import sys

# ---------------- PATH SETUP ---------------- #

BASE_DIR = os.path.dirname(os.path.abspath(__file__))  
DATASET_PATH = os.path.join(BASE_DIR, "..", "..", "dataset", "behaviour_dataset.csv")

# Ensure dataset exists
if not os.path.exists(DATASET_PATH):
    print(f"❌ Dataset not found at {DATASET_PATH}")
    sys.exit(1)

# ---------------- LOAD DATASET ---------------- #

df = pd.read_csv(DATASET_PATH)
print(f"✅ Dataset loaded successfully ({df.shape[0]} rows)")

# ---------------- FEATURE ENGINEERING ---------------- #

# 1. Productive Ratio
df["productive_ratio"] = df["total_productive_time"] / (
    df["total_productive_time"] + df["total_distracting_time"] + 1
)

# 2. Switch Frequency
if "switch_count" in df.columns:
    df["switch_frequency"] = df["switch_count"] / (
        df["total_productive_time"] + df["total_distracting_time"] + 1
    )
else:
    # Temporary simulation (since raw switch tracking not available)
    df["switch_frequency"] = 0.05

# 3. Average Session Duration
if "session_count" in df.columns:
    df["avg_session_duration"] = (
        df["total_productive_time"] + df["total_distracting_time"]
    ) / (df["session_count"] + 1)
else:
    # Assume 5 sessions if not available
    df["avg_session_duration"] = (
        df["total_productive_time"] + df["total_distracting_time"]
    ) / 5

print("✅ Feature engineering completed")

# ---------------- LABEL CREATION ---------------- #

def label_focus(score):
    if score >= 0.6:
        return "high"
    elif score >= 0.4:
        return "medium"
    else:
        return "low"

df["next_day_focus_level"] = df["focus_score"].apply(label_focus)

print("✅ 'next_day_focus_level' column added")

# ---------------- SAVE UPDATED DATASET ---------------- #

df.to_csv(DATASET_PATH, index=False)

print("✅ Dataset updated successfully with new features and labels")
print("Updated Columns:", df.columns.tolist())