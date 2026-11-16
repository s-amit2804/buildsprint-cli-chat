"""validator.py - turns an untrusted request (from the user or the LLM) into a safe, typed CrawlTask.

Nothing reaches the CLI unless it passed through validate_task(). The CLI re-checks the URL itself
(defense in depth); this layer exists to reject bad input early with a clear, chat-friendly message.
"""

import ipaddress
import socket
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

LIMITS = {
    "max_pages": (1, 20, 5),   # (min, max, default) - out-of-range values are clamped
    "max_depth": (0, 3, 1),
}


class InvalidRequest(Exception):
    """Raised with a message that is safe to show in the chat."""


def _is_public(ip):
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_public_host(hostname):
    """Reject hosts that are, or resolve to, non-public addresses (SSRF guard).

    An unresolvable name is NOT rejected here: the CLI will report it honestly as 'unreachable'.
    """
    try:
        literal = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [literal]
    else:
        if hostname.lower() == "localhost" or hostname.lower().endswith(".localhost"):
            raise ValueError("localhost is not allowed")
        try:
            infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError):
            return
        addresses = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
    for ip in addresses:
        if not _is_public(ip):
            raise ValueError(f"'{hostname}' points to a non-public address ({ip}); only public websites can be crawled")


def _clamp(name, value):
    lo, hi, _ = LIMITS[name]
    return max(lo, min(hi, value))


class CrawlTask(BaseModel):
    """The only shape of input the runner accepts. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)
    max_pages: int = LIMITS["max_pages"][2]
    max_depth: int = LIMITS["max_depth"][2]
    wants_json: bool = True

    @field_validator("url")
    @classmethod
    def _check_url(cls, v):
        v = v.strip()
        if any(c.isspace() for c in v):
            raise ValueError("URL must not contain spaces")
        try:
            p = urlparse(v)
            p.port
        except ValueError:
            raise ValueError("URL is malformed")
        if p.scheme not in ("http", "https"):
            raise ValueError("only http:// and https:// URLs are allowed")
        if not p.hostname:
            raise ValueError("URL has no hostname")
        if p.username or p.password:
            raise ValueError("URLs with embedded credentials are not allowed")
        check_public_host(p.hostname)
        return v

    @field_validator("max_pages")
    @classmethod
    def _cap_pages(cls, v):
        return _clamp("max_pages", v)

    @field_validator("max_depth")
    @classmethod
    def _cap_depth(cls, v):
        return _clamp("max_depth", v)


def validate_task(data):
    """dict -> CrawlTask, or raise InvalidRequest with a readable reason."""
    try:
        return CrawlTask.model_validate(data)
    except ValidationError as e:
        reasons = []
        for err in e.errors():
            field = ".".join(str(x) for x in err["loc"]) or "request"
            msg = err["msg"].removeprefix("Value error, ")
            reasons.append(f"{field}: {msg}")
        raise InvalidRequest("; ".join(reasons))
