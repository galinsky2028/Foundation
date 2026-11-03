"""Email. Backends: console (prints and keeps a copy in data/outbox) and smtp."""
from __future__ import annotations

import logging
import mimetypes
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)


def send_email(to: str, subject: str, body: str, html: str | None = None, attachments: list[Path] | None = None,
               reply_to: str | None = None) -> dict:
    if not to:
        raise ValueError("No recipient")
    msg = EmailMessage()
    msg["From"] = settings.email_from or settings.smtp_user or f"no-reply@{settings.app_name.lower().replace(' ', '')}.local"
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for path in attachments or []:
        ctype = mimetypes.guess_type(Path(path).name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(Path(path).read_bytes(), maintype=maintype, subtype=subtype, filename=Path(path).name)
    if settings.email_backend == "smtp":
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60) as s:
            if settings.smtp_starttls:
                s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        log.info("email sent to %s: %s", to, subject)
        return {"backend": "smtp", "to": to}
    outbox = settings.data_dir / "outbox"
    outbox.mkdir(exist_ok=True)
    path = outbox / f"{time.time_ns()}.eml"
    path.write_bytes(bytes(msg))
    print(f"\n===== EMAIL to {to}: {subject} =====\n{body}\n===== ({path}) =====\n")
    return {"backend": "console", "to": to, "file": str(path)}
