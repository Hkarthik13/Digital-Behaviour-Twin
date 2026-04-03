import pandas as pd
import numpy as np
import os
from datetime import datetime, timedelta

np.random.seed(42)

NUM_USERS = 25
DAYS_PER_USER = 40

rows = []

for user in range(1, NUM_USERS + 1):

    prev_focus = np.random.uniform(0.45, 0.65)
    fatigue = np.random.uniform(0.0, 0.3)
    user_baseline = np.random.uniform(0.45, 0.65)

    start_date = datetime(2024, 1, 1)

    for day in range(DAYS_PER_USER):

        current_date = start_date + timedelta(days=day)
        day_name = current_date.strftime("%A")

        weekend_boost = 0.1 if day_name in ["Saturday", "Sunday"] else 0

        # -----------------------------
        # Behaviour Generation
        # -----------------------------

        productive_time = np.clip(
            np.random.normal(5 + prev_focus * 4, 1.0),
            1, 10
        )

        distracting_time = np.clip(
            np.random.normal(3 + fatigue * 2, 1.0),
            0, 8
        )

        productive_ratio = productive_time / (productive_time + distracting_time + 0.1)

        switch_frequency = np.random.randint(5, 30)
        avg_session_duration = np.random.uniform(20, 60)
        peak_productive_hour = np.random.randint(8, 22)
        session_avg = np.random.uniform(30, 90)

        # -----------------------------
        # Non-linear Behaviour Signal
        # -----------------------------

        behavior_signal = (
            0.6 * productive_ratio -
            0.4 * (switch_frequency / 30)
        )

        behavior_signal = np.tanh(behavior_signal)

        # -----------------------------
        # Fatigue Update
        # -----------------------------

        fatigue = np.clip(
            fatigue + (0.5 - productive_ratio) * 0.25 + np.random.normal(0, 0.04),
            0,
            1
        )

        # -----------------------------
        # Focus Dynamics (Improved)
        # -----------------------------

        mean_reversion = 0.15 * (0.55 - prev_focus)

        noise = np.random.normal(0, 0.04)

        focus_today = (
            0.35 * prev_focus +
            0.35 * behavior_signal +
            0.15 * user_baseline +
            0.10 * weekend_boost -
            0.05 * fatigue +
            mean_reversion +
            noise
        )

        focus_today = np.clip(focus_today, 0, 1)

        rows.append([
            user,
            current_date.strftime("%Y-%m-%d"),
            day_name,
            productive_time,
            distracting_time,
            focus_today,
            peak_productive_hour,
            session_avg,
            productive_ratio,
            switch_frequency,
            avg_session_duration
        ])

        prev_focus = focus_today

columns = [
    "user_id",
    "date",
    "day_of_week",
    "total_productive_time",
    "total_distracting_time",
    "focus_today",
    "peak_productive_hour",
    "session_avg",
    "productive_ratio",
    "switch_frequency",
    "avg_session_duration"
]

df = pd.DataFrame(rows, columns=columns)

# -----------------------------
# Temporal Target Creation
# -----------------------------

df["next_day_focus"] = df.groupby("user_id")["focus_today"].shift(-1)

# -----------------------------
# Lag Features (Optional but Powerful)
# -----------------------------

df["prev_day_switch"] = df.groupby("user_id")["switch_frequency"].shift(1)
df["prev_day_ratio"] = df.groupby("user_id")["productive_ratio"].shift(1)

df = df.dropna()

# -----------------------------
# Save Dataset
# -----------------------------

os.makedirs("dataset", exist_ok=True)
output_path = os.path.join("dataset", "behaviour_dataset.csv")
df.to_csv(output_path, index=False)

print("Improved realistic temporal dataset generated successfully!")