"""Gmail integration: harvest B2B contacts from, and send outreach mail to,
the user's own inbox."""

from gmail.campaign import (
    CampaignPlan,
    CampaignResult,
    RecipientPlan,
    build_plan,
    daily_sent_count,
    mark_do_not_contact,
    send_campaign,
    unsubscribe_by_email,
)
from gmail.client import GmailClient, MailMessage, gmail_session
from gmail.harvester import (
    GmailHarvestSummary,
    HarvestCandidate,
    HarvestScan,
    harvest_inbox,
    message_to_record,
    scan_for_contacts,
    store_candidates,
)
from gmail.sender import SmtpSender
from gmail.templates import (
    MailTemplate,
    available_placeholders,
    list_templates,
    load_template,
    render,
    save_template,
)

__all__ = [
    "CampaignPlan",
    "CampaignResult",
    "GmailClient",
    "GmailHarvestSummary",
    "HarvestCandidate",
    "HarvestScan",
    "MailMessage",
    "MailTemplate",
    "RecipientPlan",
    "SmtpSender",
    "available_placeholders",
    "build_plan",
    "daily_sent_count",
    "gmail_session",
    "harvest_inbox",
    "list_templates",
    "load_template",
    "mark_do_not_contact",
    "message_to_record",
    "render",
    "save_template",
    "scan_for_contacts",
    "send_campaign",
    "store_candidates",
    "unsubscribe_by_email",
]
