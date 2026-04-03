def rule_engine(prediction_result):

    if prediction_result["focus_level"] == "high":
        return "Highly Focused - Keep it up"

    elif prediction_result["focus_level"] == "medium":
        return "Moderately Focused - Reduce app switching"

    else:
        return "Distracted - Try productivity mode"