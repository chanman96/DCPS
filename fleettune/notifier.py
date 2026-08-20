"""
Alert delivery via email (SMTP).

FleetTune's core loop makes no external network calls — that stays true here too, unless
an operator explicitly configures SMTP credentials (via the admin panel, or the
SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / ALERT_EMAIL_TO env vars). Sends are
fire-and-forget on a daemon thread so a slow or failing SMTP call never stalls the 10 Hz
sim loop, and each (vehicle, alert kind) is rate-limited so a persistent condition sends
one email, not one per analyzer tick.

Uses stdlib smtplib/email — no external dependency. Defaults suit Gmail's SMTP relay
(smtp.gmail.com:587 with STARTTLS), which needs a 16-character Google "App Password"
rather than the account's normal password when 2FA is enabled.
"""
from __future__ import annotations
import os
import smtplib
import threading
import time
from email.mime.text import MIMEText

SEVERITY_ORDER = {"info": 0, "warn": 1, "critical": 2}
RESEND_COOLDOWN_S = 300   # don't re-send the same (vehicle, alert kind) more than once per 5 min


class EmailNotifier:
    def __init__(self):
        self.smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        self.smtp_user = os.environ.get("SMTP_USER", "")
        self.smtp_password = os.environ.get("SMTP_PASSWORD", "")
        self.to_addr = os.environ.get("ALERT_EMAIL_TO", "")
        self.min_severity = "warn"
        self.sent_count = 0
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._last_sent: dict[str, float] = {}
        self._refresh_enabled()

    def _refresh_enabled(self):
        self.enabled = bool(self.smtp_host and self.smtp_user and self.smtp_password and self.to_addr)

    def configure(self, smtp_host=None, smtp_port=None, smtp_user=None, smtp_password=None,
                  to_addr=None, min_severity=None):
        if smtp_host is not None: self.smtp_host = smtp_host
        if smtp_port is not None: self.smtp_port = int(smtp_port)
        if smtp_user is not None: self.smtp_user = smtp_user
        if smtp_password is not None: self.smtp_password = smtp_password
        if to_addr is not None: self.to_addr = to_addr
        if min_severity is not None: self.min_severity = min_severity
        self._refresh_enabled()

    def status(self) -> dict:
        return {
            "configured": self.enabled,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "smtp_user": self.smtp_user,
            "smtp_password_set": bool(self.smtp_password),
            "to_addr": self.to_addr,
            "min_severity": self.min_severity,
            "sent_count": self.sent_count,
            "last_error": self.last_error,
        }

    def _should_send(self, severity: str) -> bool:
        return SEVERITY_ORDER.get(severity, 0) >= SEVERITY_ORDER.get(self.min_severity, 1)

    def notify_alert(self, alert):
        """Called by Analyzer the moment a NEW alert is created (not on every re-touch)."""
        if not self.enabled or not self._should_send(alert.severity):
            return
        key = f"{alert.vehicle_id}:{alert.kind}"
        now = time.time()
        with self._lock:
            last = self._last_sent.get(key, 0)
            if now - last < RESEND_COOLDOWN_S:
                return
            self._last_sent[key] = now
        subject = f"[FleetTune] {alert.severity.upper()} · {alert.vehicle_id} · {alert.title}"
        body = (
            f"{alert.title}\n\n{alert.detail}\n\n"
            f"Vehicle: {alert.vehicle_id}\nSeverity: {alert.severity}\nCategory: {alert.category}"
        )
        threading.Thread(target=self._send, args=(subject, body), daemon=True).start()

    def send_test(self) -> dict:
        if not self.enabled:
            return {"ok": False, "error": "not configured"}
        self._send("[FleetTune] Test alert", "Email alerting is configured correctly.")
        return {"ok": self.last_error is None, "error": self.last_error}

    def _send(self, subject: str, body: str):
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = self.smtp_user
        msg["To"] = self.to_addr
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.smtp_user, [self.to_addr], msg.as_string())
            with self._lock:
                self.sent_count += 1
                self.last_error = None
        except Exception as e:
            with self._lock:
                self.last_error = str(e)
