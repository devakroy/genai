import argparse
import sys
import smtplib
from email.message import EmailMessage


def send_email(
    smtp_host: str,
    smtp_port: int,
    sender: str,
    recipients: list[str],
    subject: str,
    body: str,
    starttls: bool = False,
    username: str | None = None,
    password: str | None = None,
    timeout_seconds: int = 20,
) -> None:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout_seconds) as smtp:
        # Opportunistically upgrade to TLS if requested (commonly required on port 587)
        if starttls:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
        if username:
            smtp.login(username, password or "")
        smtp.send_message(msg)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a test email (including to Slack channel email).")
    parser.add_argument("--smtp-host", required=True, help="SMTP relay host (e.g., smtp-nonprod.shd.aws.chewy.cloud)")
    parser.add_argument("--smtp-port", type=int, default=25, help="SMTP port (25 for relay, 587 for submission)")
    parser.add_argument("--sender", required=True, help="Sender email address")
    parser.add_argument(
        "--to",
        dest="recipients",
        nargs="+",
        required=True,
        help="One or more recipient email addresses (space-separated or repeated)",
    )
    parser.add_argument("--subject", required=True, help="Email subject")
    parser.add_argument("--body", required=True, help="Email body text")
    parser.add_argument("--starttls", action="store_true", help="Use STARTTLS (typically for port 587)")
    parser.add_argument("--username", help="SMTP username (if authentication is required)")
    parser.add_argument("--password", help="SMTP password (if authentication is required)")
    parser.add_argument("--timeout", type=int, default=20, help="Socket timeout in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        send_email(
            smtp_host=args.smtp_host,
            smtp_port=args.smtp_port,
            sender=args.sender,
            recipients=args.recipients,
            subject=args.subject,
            body=args.body,
            starttls=args.starttls,
            username=args.username,
            password=args.password,
            timeout_seconds=args.timeout,
        )
        print("Successfully sent email.")
        return 0
    except Exception as exc:
        print(f"Error occurred: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


