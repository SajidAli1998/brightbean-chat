"""Instagram through Facebook Login for Business: the Graph half of the connect flow.

The **Instagram API with Facebook Login**. The Instagram professional account
is reached through the Facebook Page it is linked to: the operator signs in to
Facebook, grants the page, and the connection holds that page's access token.
Every call then goes to graph.facebook.com, and deliveries arrive at the same
``/webhooks/instagram/`` endpoint signed by the **Facebook** app — the one
``PLATFORM_MESSENGER_*`` configures. There is no separate Instagram app id or
secret.

It exists alongside Instagram Login (:mod:`apps.channels.instagram_oauth`), not
instead of it. Instagram Login needs no Facebook Page and is the simpler path
when Meta offers it; this one works for any account linked to a page, and is
the only one available to an app whose dashboard shows only "API setup with
Facebook login".

The login, the token exchange and ``/me/accounts`` are Messenger's — the same
Facebook app, the same dialog — so this module only adds what differs: the
scopes, the redirect URI, the state purpose, and reading each page's linked
Instagram account. Which variant a connection is lives in its credentials
(``instagram_oauth.LOGIN_KEY``), and :mod:`apps.channels.providers.instagram`
reads it to pick the host and the webhook secret.
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from django.conf import settings
from django.urls import reverse

from apps.channels import instagram_oauth, messenger_oauth

__all__ = [
    "SCOPES",
    "STATE_PURPOSE",
    "InstagramAccount",
    "authorize_url",
    "callback_url",
    "credentials_for",
    "exchange_code",
    "list_accounts",
    "subscribe_page",
]

#: What the dialog asks for. ``instagram_basic`` reads the account,
#: ``instagram_manage_messages`` and ``instagram_manage_comments`` are the DM and
#: comment halves of the channel. The page scopes are what make the linked
#: account reachable at all: ``pages_show_list`` lists the page,
#: ``pages_read_engagement`` reads its ``instagram_business_account``,
#: ``pages_manage_metadata`` subscribes it to this app's webhook, and
#: ``business_management`` is what includes a page owned by a business
#: portfolio (see ``messenger_oauth.SCOPES``).
SCOPES: tuple[str, ...] = (
    "instagram_basic",
    "instagram_manage_messages",
    "instagram_manage_comments",
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_metadata",
    "business_management",
)

#: Its own signer salt, so a state minted for Messenger's connect flow cannot be
#: replayed against this one's callback, or the reverse.
STATE_PURPOSE = "instagram-facebook-oauth"

#: The fields ``/me/accounts`` is asked for. The page token comes with the page.
ACCOUNT_FIELDS = "id,name,access_token,instagram_business_account{id,username}"

MAX_USERNAME_CHARS = 100


@dataclass(frozen=True)
class InstagramAccount:
    """One Instagram professional account, and the page it is linked to.

    ``page_token`` is the credential the connection ends up holding, so — like
    ``messenger_oauth.MetaPage`` — this is never rendered, logged or put in a
    session.
    """

    id: str
    username: str
    page_id: str
    page_name: str
    page_token: str


def callback_url() -> str:
    """The absolute redirect URI. Whitelisted on the Facebook app, next to Messenger's."""
    return urljoin(settings.APP_URL.rstrip("/") + "/", reverse("instagram_facebook_callback").lstrip("/"))


def authorize_url(*, client_id: str, state: str) -> str:
    return messenger_oauth.authorize_url(client_id=client_id, state=state, scopes=SCOPES, redirect_uri=callback_url())


def exchange_code(*, code: str, client_id: str, client_secret: str) -> str:
    """The long-lived user token. See ``messenger_oauth.exchange_code``."""
    return messenger_oauth.exchange_code(
        code=code, client_id=client_id, client_secret=client_secret, redirect_uri=callback_url()
    )


def list_accounts(user_token: str) -> list[InstagramAccount]:
    """The Instagram accounts linked to the pages this person granted.

    A page with no linked Instagram account is left out, as is one Meta returned
    without a token: neither is something the operator could connect.
    """
    from apps.channels.providers.messenger import BACKGROUND_TIMEOUT, bounded_id, graph_call

    body = graph_call(
        user_token,
        "GET",
        "me/accounts",
        params={"fields": ACCOUNT_FIELDS, "limit": str(messenger_oauth.MAX_PAGES)},
        timeout=BACKGROUND_TIMEOUT,
    )
    rows = body.get("data")
    if not isinstance(rows, list):
        return []

    accounts: list[InstagramAccount] = []
    for item in rows[: messenger_oauth.MAX_PAGES]:
        if not isinstance(item, dict):
            continue
        linked = item.get("instagram_business_account")
        linked = linked if isinstance(linked, dict) else {}
        token = item.get("access_token")
        page_id = bounded_id(item.get("id"))
        account_id = bounded_id(linked.get("id"))
        if not account_id or not page_id or not isinstance(token, str) or not token:
            continue
        username = linked.get("username")
        name = item.get("name")
        accounts.append(
            InstagramAccount(
                id=account_id,
                username=(username.strip()[:MAX_USERNAME_CHARS] if isinstance(username, str) else "") or account_id,
                page_id=page_id,
                page_name=(name.strip()[: messenger_oauth.MAX_PAGE_NAME_CHARS] if isinstance(name, str) else ""),
                page_token=token,
            )
        )
    return accounts


def credentials_for(account: InstagramAccount) -> dict[str, Any]:
    """What the connection stores. The token under Instagram Login's own key, so
    every send path reads it unchanged; the variant and the page beside it."""
    return {
        instagram_oauth.TOKEN_KEY: account.page_token,
        instagram_oauth.LOGIN_KEY: instagram_oauth.FACEBOOK_LOGIN,
        instagram_oauth.PAGE_ID_KEY: account.page_id,
    }


def subscribe_page(account: InstagramAccount) -> None:
    """Install this app on the linked page, which Instagram deliveries require.

    The same fields Messenger subscribes, deliberately: ``subscribed_fields``
    replaces the page's set rather than adding to it, so asking for anything
    narrower would cut off a Messenger connection on the same page.
    """
    from apps.channels.providers.messenger import BACKGROUND_TIMEOUT, SUBSCRIBED_FIELDS, graph_call

    graph_call(
        account.page_token,
        "POST",
        f"{account.page_id}/subscribed_apps",
        params={"subscribed_fields": ",".join(SUBSCRIBED_FIELDS)},
        timeout=BACKGROUND_TIMEOUT,
    )
