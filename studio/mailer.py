"""Outgoing email over SMTP (stdlib). Without PF_SMTP_HOST, messages are printed to the console instead,
so password resets can be tried locally before any mail provider is set up."""
import smtplib
import ssl
import traceback
from email.message import EmailMessage
from email.utils import make_msgid

from . import config


def configured() -> bool:
    return bool(config.settings.smtp_host)


def sender() -> str:
    s = config.settings
    return s.mail_from or s.smtp_user or "Photo Finder Studio <no-reply@localhost>"


def send(to: str, subject: str, text: str, html: str = "") -> bool:
    """Send one message; never raises (it runs after the response). Returns True when handed to the server."""
    s = config.settings
    if not configured():
        print(f"\n--- email (SMTP not configured, PF_SMTP_HOST unset) ---\nTo: {to}\nSubject: {subject}\n\n{text}\n---\n",
              flush=True)
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender(), to, subject
    msg["Message-ID"] = make_msgid()
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        ctx = ssl.create_default_context()
        if s.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=20, context=ctx)
        else:
            server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20)
        with server:
            if s.smtp_security == "starttls":
                server.starttls(context=ctx)
            if s.smtp_user:
                server.login(s.smtp_user, s.smtp_password)
            server.send_message(msg)
        return True
    except Exception:  # a mail outage must not break the request; the log shows why
        traceback.print_exc()
        return False
