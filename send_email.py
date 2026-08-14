"""
send_email.py
-------------
Emails the daily analysis digest as an Excel attachment. Same pattern as
srikanth-stock's send_email.py: Gmail SMTP with an App Password, read from
GitHub Actions secrets — no credentials ever live in this file or in git.

Reads from environment variables:
    GMAIL_ADDRESS       the Gmail address to send from   (required)
    GMAIL_APP_PASSWORD  a Gmail App Password              (required)
    RECIPIENT_EMAIL     who receives the email             (defaults to GMAIL_ADDRESS)

Run:
    python send_email.py --file daily_digest.xlsx
"""
import argparse
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


def build_message(sender: str, recipient: str, attachment: Path, buy_count: int) -> EmailMessage:
    msg = EmailMessage()
    status = f"{buy_count} BUY signal(s)" if buy_count else "no BUY signals"
    msg["Subject"] = f"Bourse — Daily Analysis Digest ({status})"
    msg["From"] = sender
    msg["To"] = recipient

    msg.set_content(
        "Attached is today's live NSE analysis digest — Scout/Technician/Fundamentalist/"
        "Newsdesk/Bull/Bear/Judge pipeline, run on GitHub Actions with real yfinance data.\n\n"
        "Analysis only. No trades were placed. Not investment advice."
    )
    if attachment.exists():
        msg.add_attachment(
            attachment.read_bytes(),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=attachment.name,
        )
    return msg


def main():
    parser = argparse.ArgumentParser(description="Email the daily digest")
    parser.add_argument("--file", default="daily_digest.xlsx",
                         help="path to the Excel file produced by run_daily.py")
    parser.add_argument("--buy-count", type=int, default=int(os.environ.get("BUY_COUNT", "0")),
                         help="number of BUY signals fired, for the subject line")
    args = parser.parse_args()

    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ.get("RECIPIENT_EMAIL", sender)

    msg = build_message(sender, recipient, Path(args.file), args.buy_count)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)

    print(f"Email sent to {recipient}")


if __name__ == "__main__":
    main()
