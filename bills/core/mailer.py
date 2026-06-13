"""Shared SMTP mailer that attaches a single PDF invoice."""

from __future__ import annotations

import os
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from ..config import MailConfig


class Mailer:
    def __init__(self, cfg: MailConfig) -> None:
        self.cfg = cfg

    def send_pdf(self, pdf_path: str, subject: str, body: str) -> bool:
        if not self.cfg.usable:
            print(
                "  email skipped: SMTP not configured "
                "(set *_SMTP_SERVER / *_EMAIL_FROM / *_EMAIL_PASSWORD / *_EMAIL_TO)",
                flush=True,
            )
            return False

        filename = os.path.basename(pdf_path)
        msg = MIMEMultipart()
        msg["From"] = self.cfg.sender
        msg["To"] = self.cfg.recipient
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        try:
            with open(pdf_path, "rb") as fh:
                part = MIMEBase("application", "pdf")
                part.set_payload(fh.read())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition", "attachment", filename=filename
                )
                msg.attach(part)

            with smtplib.SMTP(self.cfg.server, self.cfg.port) as server:
                server.starttls()
                server.login(self.cfg.sender, self.cfg.password)
                server.send_message(msg)

            print(f"  emailed {filename} -> {self.cfg.recipient}", flush=True)
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"  email failed: {exc}", flush=True)
            return False
