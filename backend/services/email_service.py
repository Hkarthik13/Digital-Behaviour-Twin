import smtplib
from email.mime.text import MIMEText
import os

def send_alert_email(to_email, message):
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")

    
    if not sender_email or not sender_password:
        print(f"\n📧 [EMAIL ALERT MOCKED]")
        print(f"To: {to_email}")
        print(f"Message: {message}\n")
        return

    try:
        msg = MIMEText(message)
        msg['Subject'] = 'Digital Behaviour Twin - Alert'
        msg['From'] = sender_email
        msg['To'] = to_email

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, to_email, msg.as_string())
        server.quit()
        print(f"Email sent successfully to {to_email}")
    except Exception as e:
        print(f"Email failed: {e}")