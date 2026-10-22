import smtplib
import asyncio
from email.mime.text import MIMEText
from config import SMTP_SERVER, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO

def send_email_sync(subject, body):
    """Synchronous function to send an email using smtplib."""
    if not all([SMTP_USERNAME, SMTP_PASSWORD, EMAIL_TO]):
        print("Email configuration is incomplete. Skipping email send.")
        return

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_TO

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        print(f"Successfully sent email: {subject}")
    except Exception as e:
        print(f"Failed to send email: {e}")

async def send_email_async(subject, body):
    """Asynchronously run the blocking email function."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, send_email_sync, subject, body)
