"""Standard-library SMTP adapter for JobIntel notification email."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from jobintel.notifications.address import validate_email_address
from jobintel.notifications.models import SMTPTransport


class SMTPEmailSender:
    """Send prepared messages through one configured SMTP account or relay."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        transport: SMTPTransport,
        from_address: str,
        recipient: str,
        username: str | None = None,
        password: str | None = None,
        timeout_seconds: float = 15,
    ) -> None:
        """Validate and retain SMTP connection and recipient settings."""
        self.host = host.strip()
        if not self.host:
            raise ValueError("SMTP host is required")
        if not 1 <= port <= 65535:
            raise ValueError("SMTP port must be between 1 and 65535")
        if timeout_seconds <= 0:
            raise ValueError("SMTP timeout must be positive")
        self.port = port
        self.transport = transport
        self.from_address = validate_email_address(from_address, label="sender")
        self.recipient = validate_email_address(recipient, label="recipient")
        self.username = username.strip() if username else None
        self.password = password
        if (self.username is None) != (self.password is None):
            raise ValueError("SMTP username and password must be configured together")
        if transport is SMTPTransport.PLAIN and self.username is not None:
            raise ValueError("SMTP authentication requires starttls or ssl transport")
        self.timeout_seconds = timeout_seconds

    def send(self, message: EmailMessage) -> None:
        """Deliver one message using TLS verification and optional authentication."""
        context = ssl.create_default_context()
        if self.transport is SMTPTransport.SSL:
            with smtplib.SMTP_SSL(
                self.host, self.port, timeout=self.timeout_seconds, context=context
            ) as client:
                self._authenticate_and_send(client, message)
            return
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout_seconds) as client:
            if self.transport is SMTPTransport.STARTTLS:
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            self._authenticate_and_send(client, message)

    def _authenticate_and_send(
        self, client: smtplib.SMTP | smtplib.SMTP_SSL, message: EmailMessage
    ) -> None:
        """Authenticate when configured and send to the fixed recipient."""
        if self.username is not None and self.password is not None:
            client.login(self.username, self.password)
        client.send_message(
            message,
            from_addr=self.from_address,
            to_addrs=[self.recipient],
        )
