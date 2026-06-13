"""Configurable invoice email subject/body templates."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config

_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<provider>[^/\\]+?)(?:\s+(?P<number>[^\s/\\]+))?\.pdf$",
    re.IGNORECASE,
)

DEFAULT_SUBJECT = "{provider} invoice: {filename}"
DEFAULT_BODY = (
    "Attached is the latest {provider} invoice: {filename}\n"
    "Date: {date}\n"
    "Invoice number: {number}"
)

PLACEHOLDER_HELP = "{provider}, {addon}, {filename}, {date}, {number}, {recipient}"


class _SafeFormat(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_template(template: str, **values: str) -> str:
    return str(template).format_map(_SafeFormat(**values))


def parse_invoice_filename(filename: str) -> tuple[str, str, str]:
    match = _FILENAME_RE.match(filename)
    if match:
        return match.group("date"), match.group("provider"), match.group("number") or ""
    return "", "", ""


def invoice_mail_message(
    cfg: Config,
    *,
    addon: str,
    provider: str,
    filename: str,
) -> tuple[str, str]:
    date, _parsed_provider, number = parse_invoice_filename(filename)
    mail_cfg = cfg.mail_for(addon)
    context = {
        "provider": provider,
        "addon": addon,
        "filename": filename,
        "date": date or "—",
        "number": number or "—",
        "recipient": mail_cfg.recipient or "—",
    }
    subject_tpl = cfg.get("BILLS_MAIL_SUBJECT", DEFAULT_SUBJECT) or DEFAULT_SUBJECT
    body_tpl = cfg.get("BILLS_MAIL_BODY", DEFAULT_BODY) or DEFAULT_BODY
    return (
        render_template(subject_tpl, **context),
        render_template(body_tpl, **context),
    )
