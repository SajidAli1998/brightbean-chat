"""Messenger sender profiles: the name, photo, locale and timezone Meta holds.

A Messenger webhook names its sender by PSID and nothing else — unlike
Telegram, whose updates carry ``first_name``, or WhatsApp, whose carry
``contacts[].profile.name``. The person's name has to be asked for separately,
through the User Profile API (``GET /<PSID>?fields=...`` with the page token).
Until this existed nobody asked, so every Messenger contact was listed as
``Contact 01a11ae4``.

Two halves, so the webhook never waits on Meta:

* :func:`profile_events` is a late stage on contract 6's seam. It runs after
  persistence has resolved the identity and, for a Messenger identity whose
  profile has not been fetched, queues one :data:`ACTION_TYPE` row.
* :func:`fetch_profile` is that row's handler, on the worker. It makes the call
  and writes what came back.

What comes back lands in two places. The identity's ``extra`` always gets it
(``profile_pic_url`` is what :func:`apps.contacts.activity.avatar_url` reads).
The contact's own columns get it **only where they are blank**: an operator who
typed a name, or an import that supplied one, is never overwritten by what a
platform says.

Meta gates the fields separately. ``first_name``, ``last_name`` and
``profile_pic`` need the *Business Asset User Profile Access* feature;
``locale`` and ``timezone`` need ``pages_user_locale`` and
``pages_user_timezone``, which a page granted before those scopes were
requested does not have. A refusal of the full field list is therefore retried
once with the basic fields, so a page that has not reconnected still gets names.
"""

import logging
from collections.abc import Sequence
from typing import Any

from django.utils import timezone

from apps.channels.events import EventType, NormalizedEvent
from apps.channels.models import ChannelConnection
from apps.common.platforms import Platform

logger = logging.getLogger(__name__)

PROFILE_PROCESSOR = "messenger_profile"
ACTION_TYPE = "messenger_fetch_profile"

#: Everything the User Profile API will tell us that a contact has a column for.
FULL_FIELDS = "first_name,last_name,profile_pic,locale,timezone"
#: What the *Business Asset User Profile Access* feature alone allows.
BASIC_FIELDS = "first_name,last_name,profile_pic"

#: Set on the identity's ``extra`` once a fetch has succeeded. Its presence is
#: what stops every later message from queueing another lookup.
FETCHED_KEY = "profile_fetched_at"

#: Events whose ``platform_user_id`` is a PSID. A comment's author id is not —
#: it is not addressable through the User Profile API.
PROFILE_EVENTS = frozenset({EventType.MESSAGE, EventType.POSTBACK, EventType.REFERRAL})

#: Graph error codes meaning "a permission for one of these fields is missing".
#: Strings, because ``APIError.code`` is the code as ``request_json`` lifted it.
PERMISSION_CODES = frozenset({"10", "200"})

#: Contact columns the profile fills, and the bound each is cleaned to. The
#: bounds match ``apps.contacts.services._SCALAR_LIMITS``.
_LIMITS = {"first_name": 150, "last_name": 150, "locale": 16}


def register() -> None:
    """Register the seam stage and the queue handler. Called from ``ready()``."""
    from apps.channels import ingest
    from apps.queueing.registry import register_handler

    ingest.register_processor(profile_events, name=PROFILE_PROCESSOR, order=ingest.LATE_ORDER)
    register_handler(ACTION_TYPE, replace=True)(fetch_profile)


def profile_events(connection: ChannelConnection, events: Sequence[NormalizedEvent]) -> None:
    """Queue a profile lookup for each Messenger sender we have not looked up.

    Never raises. A name is decoration, and a raising stage fails the whole
    batch — the same trade ``apps.messaging.ingest._record_display_fields``
    refuses to make.
    """
    if connection.platform != Platform.MESSENGER:
        return
    psids = {event.platform_user_id for event in events if event.type in PROFILE_EVENTS and event.platform_user_id}
    if not psids:
        return
    try:
        _queue_lookups(connection, psids)
    except Exception:
        logger.exception("Could not queue Messenger profile lookups on connection %s.", connection.pk)


def _queue_lookups(connection: ChannelConnection, psids: set[str]) -> None:
    from apps.messaging.models import ContactChannelIdentity
    from apps.queueing.registry import schedule

    identities = ContactChannelIdentity.objects.for_workspace(connection.workspace_id).filter(
        channel_connection=connection, platform_user_id__in=psids
    )
    now = timezone.now()
    for identity in identities:
        extra = identity.extra if isinstance(identity.extra, dict) else {}
        if extra.get(FETCHED_KEY):
            continue
        schedule(
            ACTION_TYPE,
            now,
            {"identity_id": str(identity.pk)},
            workspace=connection.workspace,
            contact=identity.contact_id,
            # One attempt per identity per day until one succeeds. A key without
            # the date would make a lookup refused for a missing permission
            # permanent, even after the page reconnects with that permission.
            idempotency_key=f"{ACTION_TYPE}:{identity.pk}:{now.date().isoformat()}",
            max_attempts=3,
        )


def fetch_profile(payload: dict[str, Any], action: Any) -> None:
    """The queue handler: ask Meta who this PSID is and write the answer down.

    Raises only for what a retry can fix — a rate limit or a Meta 5xx. Every
    other refusal is logged and the row finishes, so a page missing a permission
    does not burn three attempts per contact.
    """
    from apps.channels.providers.exceptions import APIError
    from apps.channels.providers.messenger import page_token
    from apps.messaging.models import ContactChannelIdentity

    identity = (
        ContactChannelIdentity.objects.for_workspace(action.workspace_id)
        .select_related("contact", "channel_connection")
        .filter(pk=str(payload.get("identity_id") or ""))
        .first()
    )
    if identity is None or identity.channel_connection is None:
        return
    extra = identity.extra if isinstance(identity.extra, dict) else {}
    if extra.get(FETCHED_KEY):
        return
    psid = identity.platform_user_id
    # The PSID becomes a URL path segment. It arrived in a signed delivery, but
    # a path built from anything but digits is not one this module will send.
    if not psid.isdigit():
        return
    token = page_token(identity.channel_connection)
    if not token:
        return

    try:
        body = _lookup(token, psid)
    except APIError as exc:
        if exc.status_code == 429 or (exc.status_code or 0) >= 500:
            raise
        logger.info("Meta refused a Messenger profile lookup for identity %s (code %s).", identity.pk, exc.code)
        return
    apply_profile(identity, body)


def _lookup(token: str, psid: str) -> dict[str, Any]:
    """The User Profile API call, narrowing the fields once if a permission is missing."""
    from apps.channels.providers.base import BACKGROUND_TIMEOUT
    from apps.channels.providers.exceptions import APIError
    from apps.channels.providers.messenger import graph_call

    try:
        return graph_call(token, "GET", psid, params={"fields": FULL_FIELDS}, timeout=BACKGROUND_TIMEOUT)
    except APIError as exc:
        if exc.code not in PERMISSION_CODES:
            raise
    return graph_call(token, "GET", psid, params={"fields": BASIC_FIELDS}, timeout=BACKGROUND_TIMEOUT)


def apply_profile(identity: Any, body: dict[str, Any]) -> None:
    """Write a profile onto the identity, and into the contact's blank columns."""
    from apps.contacts.services import update_contact

    values = {name: _clean(body.get(name), limit) for name, limit in _LIMITS.items()}
    values["timezone"] = etc_zone(body.get("timezone"))
    picture = body.get("profile_pic")
    picture = picture if isinstance(picture, str) and picture.startswith("https://") else ""

    stored = identity.extra if isinstance(identity.extra, dict) else {}
    identity.extra = {
        **stored,
        **{name: value for name, value in values.items() if value},
        **({"profile_pic_url": picture} if picture else {}),
        FETCHED_KEY: timezone.now().isoformat(),
    }
    identity.save(update_fields=["extra", "updated_at"])

    contact = identity.contact
    blanks = {name: value for name, value in values.items() if value and not getattr(contact, name)}
    if blanks:
        update_contact(contact, **blanks)


def etc_zone(offset: Any) -> str:
    """Meta's whole-hour UTC offset as an IANA zone name, or "".

    The User Profile API answers ``timezone`` with a number (``3``, ``-5``,
    ``5.5``). A contact's ``timezone`` column is read as an IANA name wherever
    it is read as a zone at all, so a bare number would be a value that breaks
    the first ``ZoneInfo()`` it meets. ``Etc/GMT±N`` is the IANA spelling of a
    fixed offset — with the sign **inverted**, which is POSIX's convention and
    not a bug: ``Etc/GMT-3`` is three hours *ahead* of UTC. A fractional offset
    has no ``Etc`` zone, so it is left out rather than rounded into a wrong one.
    """
    if isinstance(offset, bool) or not isinstance(offset, (int, float)):
        return ""
    if not float(offset).is_integer() or not -12 <= offset <= 14:
        return ""
    hours = int(offset)
    if hours == 0:
        return "Etc/GMT"
    return f"Etc/GMT{'-' if hours > 0 else '+'}{abs(hours)}"


def _clean(value: Any, limit: int) -> str:
    """A platform-supplied string, stripped, NUL-free and bounded. Escape on render."""
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "").strip()[:limit]
