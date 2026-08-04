"""Turn inbox messages into CRM records.

Business mail carries a signature block, and a signature block is a contact
record: company, name, title, phone, website. This module reads those out of
messages already in the user's own mailbox and feeds them through the same
cleaning and deduplication pipeline as crawled data.

Free-mail senders (``gmail.com`` and friends, configurable) are skipped -- their
domain says nothing about a company.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.config import AppConfig, get_config
from core.constants import ActivityType, LogCategory
from core.logging_setup import get_logger
from core.schemas import RawCompany
from database.repository import ActivityRepository, CompanyRepository, ContactRepository
from database.session import session_scope
from gmail.client import GmailClient, MailMessage
from verifier.dedupe import deduplicate_batch
from verifier.mx import MXChecker
from verifier.normalize import (
    normalize_company_name,
    normalize_person_name,
    normalize_phone,
    normalize_website,
)
from verifier.service import CleaningService

log = get_logger(LogCategory.CRAWL)

# Signature markers, in the order a scanner should trust them.
_SIGNATURE_MARKERS = ("-- ", "--\n", "敬祝", "順頌商祺", "Best regards", "Kind regards", "Regards,")

_COMPANY_LINE = re.compile(
    r"^(?P<name>[^\n]{2,60}?(?:股份有限公司|有限公司|企業社|工作室|實業|科技|"
    r"Co\.?,?\s*Ltd\.?|Inc\.?|Corp\.?|LLC))\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_TITLE_LINE = re.compile(
    r"^(?P<title>[^\n]{0,30}?(?:經理|總監|協理|副理|主任|工程師|業務|專員|負責人|"
    r"Manager|Director|Engineer|Sales|CEO|CTO))\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_PHONE_LINE = re.compile(
    r"(?:TEL|Tel|電話|手機|Mobile|M)\s*[:：]?\s*"
    r"((?:\+886|0)[\d\s\-()]{7,15}(?:\s*(?:#|轉|分機)\s*\d{1,6})?)"
)
_URL_LINE = re.compile(r"(https?://[^\s<>\"]+|www\.[^\s<>\"]+)")


@dataclass
class GmailHarvestSummary:
    """Outcome of one inbox scan."""

    messages_scanned: int = 0
    messages_skipped: int = 0
    records_found: int = 0
    records_new: int = 0
    records_merged: int = 0
    contacts_created: int = 0
    error: str | None = None


def _signature_block(body: str) -> str:
    """The tail of a message most likely to be the signature."""
    if not body:
        return ""
    for marker in _SIGNATURE_MARKERS:
        index = body.rfind(marker)
        if index != -1:
            return body[index : index + 1200]
    # No marker: the last few non-empty lines are the next best guess.
    lines = [line for line in body.splitlines() if line.strip()]
    return "\n".join(lines[-12:])


def message_to_record(
    message: MailMessage, ignore_domains: set[str]
) -> RawCompany | None:
    """Extract a company record from one message, or ``None`` to skip it."""
    if not message.sender_email or "@" not in message.sender_email:
        return None
    if message.sender_domain in ignore_domains:
        return None

    signature = _signature_block(message.body)

    company = message.headers.get("Organization") or ""
    if not company:
        match = _COMPANY_LINE.search(signature)
        if match:
            company = match.group("name")
    if not company:
        # Fall back to the domain: "nissin-electronics.com.tw" -> "Nissin Electronics".
        label = message.sender_domain.split(".")[0]
        company = label.replace("-", " ").replace("_", " ").title()

    phone = None
    phone_match = _PHONE_LINE.search(signature) or _PHONE_LINE.search(message.body)
    if phone_match:
        phone = normalize_phone(phone_match.group(1))

    website = None
    url_match = _URL_LINE.search(signature)
    if url_match:
        website = normalize_website(url_match.group(1))
    if not website:
        website = normalize_website(message.sender_domain)

    title_match = _TITLE_LINE.search(signature)

    return RawCompany(
        company_name=normalize_company_name(company) or company,
        email=message.sender_email,
        phone=phone,
        website=website,
        contact_person=normalize_person_name(message.sender_name),
        source="gmail",
        source_url=f"imap://{message.uid}",
        extra={
            "subject": message.subject,
            "title": title_match.group("title").strip() if title_match else None,
            "received": message.date.isoformat() if message.date else None,
        },
    )


def harvest_inbox(
    query: str | None = None,
    limit: int | None = None,
    config: AppConfig | None = None,
    client: GmailClient | None = None,
) -> GmailHarvestSummary:
    """Scan the mailbox and store what it finds.

    ``client`` is injectable so tests can supply a fake without an IMAP server.
    """
    config = config or get_config()
    summary = GmailHarvestSummary()
    ignore = set(config.gmail.ignore_domains)

    owned_client = client is None
    client = client or GmailClient(config)
    if owned_client:
        client.connect()

    try:
        records: list[RawCompany] = []
        titles: dict[str, str] = {}      # email -> job title from the signature
        names: dict[str, str] = {}       # email -> person name

        for message in client.iter_messages(query, limit):
            summary.messages_scanned += 1
            record = message_to_record(message, ignore)
            if record is None:
                summary.messages_skipped += 1
                continue
            records.append(record)
            if record.email:
                title = record.extra.get("title")
                if title:
                    titles[record.email] = title
                if record.contact_person:
                    names[record.email] = record.contact_person

        unique, _dropped = deduplicate_batch(records)
        summary.records_found = len(unique)

        with session_scope() as session:
            repo = CompanyRepository(session)
            contact_repo = ContactRepository(session)
            activity_repo = ActivityRepository(session)
            mx = MXChecker(config, session) if config.verifier.check_mx else None
            cleaner = CleaningService(config, mx)

            cleaned, _rejected = cleaner.clean_many(unique)
            # Counted by difference rather than incremented per branch: a
            # contact can now be created either here or by ``upsert`` (from the
            # parsed name), and only the total is what "how many people did
            # this harvest add" actually means.
            contacts_before = contact_repo.count()

            for record in cleaned:
                company, merged = repo.upsert(record)
                if merged:
                    summary.records_merged += 1
                else:
                    summary.records_new += 1
                    activity_repo.add(
                        company.id,
                        ActivityType.SYSTEM,
                        subject="Imported from Gmail",
                        body=f"Harvested from a message sent by {record.email}.",
                    )

                person = names.get(record.email or "")
                if person:
                    contact = next(
                        (c for c in company.contacts if (c.name or "").strip() == person),
                        None,
                    )
                    if contact is None:
                        contact = contact_repo.add(
                            company.id,
                            name=person,
                            email=record.email,
                            phone=record.phone,
                            is_primary=not company.contacts,
                        )
                    # The job title only ever appears in a mail signature, so
                    # this is the one place that can supply it -- ``upsert``
                    # may already have created the row from the parsed name.
                    if not contact.title:
                        contact.title = titles.get(record.email or "")

            session.flush()
            summary.contacts_created = contact_repo.count() - contacts_before
    finally:
        if owned_client:
            client.close()

    log.info(
        "gmail harvest: {} scanned, {} skipped, {} new, {} merged, {} contacts",
        summary.messages_scanned,
        summary.messages_skipped,
        summary.records_new,
        summary.records_merged,
        summary.contacts_created,
    )
    return summary
