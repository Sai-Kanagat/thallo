"""SMTP (your own mail server). Sends one plain-text email, only with a person's tap and the
operator's live switch on (see live_base.py). Otherwise the manual mailto link opens the seller's
own mailbox.

Where the password may go. The server comes only from the operator's environment,
THALLO_SMTP_ORIGIN = `smtps://host[:port]` (implicit TLS, default port 465; no STARTTLS, no
plain text). Connection settings are tenant-writable and hold only the login name, the sender
address and a subject. The host is resolved once, every address must be public, the socket is
pinned to the checked address, TLS checks the host name, and the password is revealed only after
the TLS session is up.
"""
from __future__ import annotations

import smtplib
import socket
import ssl
from collections.abc import Mapping
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from thallo.connections.errors import ConnectionsError
from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode, MessageChannel
from thallo.providers.messaging.live_base import LiveMessagingProvider, MessageNotSent, MessageOutcomeUnknown
from thallo.providers.messaging.resend import EMAIL
from thallo.providers.text.base import AddressPolicy, address_allowed, system_resolver

NAME = "smtp"
ORIGIN_ENV = "THALLO_SMTP_ORIGIN"


class SmtpSettings(BaseModel):
    """Tenant-writable: no host, port, URL or credential."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    login: str = Field(pattern=r"^[^\s\x00-\x1f]{1,120}$")
    from_address: str = Field(pattern=EMAIL)
    from_name: str | None = Field(default=None, pattern=r"^[^\r\n<>\"]{1,60}$")
    subject: str = Field(default="Your enquiry", pattern=r"^[^\r\n]{1,120}$")


def operator_origin(value: str | None) -> tuple[str, int]:
    """(host, port) from `smtps://host[:port]`; raises ValueError for anything else."""
    parts = urlsplit((value or "").strip())
    if parts.scheme != "smtps" or not parts.hostname or parts.username or parts.password \
            or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"{ORIGIN_ENV} must be smtps://host[:port]")
    try:
        port = parts.port or 465
    except ValueError:
        raise ValueError(f"{ORIGIN_ENV} has a bad port") from None
    return parts.hostname, port


class _PinnedSMTP(smtplib.SMTP_SSL):
    """Connects to the checked address; TLS verifies the original host name."""

    def __init__(self, host: str, ip: str, port: int, timeout: float) -> None:
        self._pin = ip
        self._tls_host = host
        super().__init__(host=None, timeout=timeout, context=ssl.create_default_context())
        self._host = host
        self.connect(host, port)

    def _get_socket(self, host, port, timeout):  # noqa: ANN001, ANN202
        raw = socket.create_connection((self._pin, port), timeout)
        return self.context.wrap_socket(raw, server_hostname=self._tls_host)


def pinned_smtp(host: str, ip: str, port: int, timeout: float) -> Any:
    return _PinnedSMTP(host, ip, port, timeout)


class Smtp(LiveMessagingProvider):
    name = NAME
    mode = ConnectionMode.HOSTED
    channels = frozenset({MessageChannel.EMAIL})
    settings_model = SmtpSettings
    licence_note = "Your own mail server; deliverability is yours to manage"

    def __init__(self, ctx: ProviderContext, *, transport: Any = None, environ: Mapping[str, str] | None = None,
                 resolver: Any = system_resolver) -> None:
        super().__init__(ctx, transport=transport or pinned_smtp, environ=environ)
        self.resolver = resolver

    def _origin(self) -> tuple[str, int]:
        import os

        env = os.environ if self.environ is None else self.environ
        return operator_origin(env.get(ORIGIN_ENV))

    def extra_problem(self) -> str | None:
        try:
            self._origin()
        except ValueError as exc:
            return str(exc)
        return None

    def send(self, request: c.MessageSendRequest) -> str:
        host, port = self._origin()
        password = self.secret()
        msg = EmailMessage()
        s = self.settings
        msg["From"] = f"{s.from_name} <{s.from_address}>" if s.from_name else s.from_address
        msg["To"] = request.to.strip()
        msg["Subject"] = s.subject
        mid = make_msgid(domain=s.from_address.rsplit("@", 1)[1])
        msg["Message-ID"] = mid
        msg.set_content(request.body)
        try:
            ips = list(self.resolver(host, port))
        except ConnectionsError:
            raise MessageNotSent("the mail server's name did not resolve") from None
        if not ips or not all(address_allowed(ip, AddressPolicy.PUBLIC) for ip in ips):
            raise MessageNotSent("the mail server resolves to an address that is not public")
        try:
            server = self.transport(host, ips[0], port, 20.0)
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            raise MessageNotSent(f"could not connect to the mail server ({type(exc).__name__})") from None
        try:
            try:
                server.login(s.login, password.reveal())
            except (OSError, smtplib.SMTPException) as exc:
                raise MessageNotSent(f"the mail server refused the login ({type(exc).__name__})") from None
            try:
                refused = server.send_message(msg)
            except smtplib.SMTPRecipientsRefused:
                raise MessageNotSent("the mail server refused the recipient") from None
            except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
                raise MessageNotSent(f"the mail server refused the message ({type(exc).__name__})") from None
            except (OSError, smtplib.SMTPException) as exc:
                raise MessageOutcomeUnknown(f"the exchange broke off ({type(exc).__name__}); it may have sent") \
                    from None
            if refused:
                raise MessageNotSent("the mail server refused the recipient")
        finally:
            try:
                server.quit()
            except (OSError, smtplib.SMTPException):
                pass
        return mid


def create(ctx: ProviderContext, transport=None, environ: Mapping[str, str] | None = None,  # noqa: ANN001
           resolver=system_resolver) -> Smtp:  # noqa: ANN001
    return Smtp(ctx, transport=transport, environ=environ, resolver=resolver)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.MESSAGING, NAME, ConnectionMode.HOSTED, create, settings_schema=SmtpSettings)
