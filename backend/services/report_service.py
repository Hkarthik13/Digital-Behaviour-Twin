def generate_ai_feedback(productive_time, distracting_time, focus_ratio, risk_score, anomalies):
    feedback = ""

    if focus_ratio > 0.8:
        feedback += "Your focus level is excellent this week. "
    elif focus_ratio > 0.6:
        feedback += "Your focus is moderate. There is room for improvement. "
    else:
        feedback += "Your focus is low. Consider reducing distractions. "

    if distracting_time > productive_time * 0.5:
        feedback += "High distracting time detected. Try limiting social media usage. "

    if risk_score > 70:
        feedback += "Risk score is high. Your browsing behaviour shows instability. "

    if anomalies > 100:
        feedback += "Large number of anomalies detected. Behaviour patterns are inconsistent. "

    feedback += "Maintain a structured schedule to improve long-term productivity."

    return feedback