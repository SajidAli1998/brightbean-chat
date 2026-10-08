"""Connect an Instagram account through Facebook Login for Business.

The screens for :mod:`apps.channels.instagram_facebook`. Messenger's connect flow
(:mod:`apps.channels.views_messenger`) in shape and in every safeguard — read its
module docstring for the reasoning — because it is the same Facebook app and the
same dialog:

``instagram/facebook/connect/``
    ``POST`` only, from the Instagram connect page. Mints a signed state and
    sends the browser to Facebook.

``/channels/instagram/facebook/callback/``
    Where Facebook comes back. Checks the state, then the signed-in user's
    membership and ``manage_channels`` on the workspace it names, exchanges the
    code, and holds the long-lived **user** token in the session, encrypted.

``instagram/facebook/accounts/``
    Pick which linked Instagram account to connect. Both methods re-read
    ``/me/accounts``, so a page token never exists outside one request.
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from apps.channels import instagram_facebook, messenger_oauth
from apps.channels.forms import DUPLICATE_ACCOUNT_ERROR
from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.channels.plan import plan_refusal
from apps.channels.providers.exceptions import APIError
from apps.channels.views_messenger import NOT_CONFIGURED, _app_credentials, _membership_or_404
from apps.common.encryption import decrypt_value, encrypt_value
from apps.common.platforms import Platform
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest

logger = logging.getLogger(__name__)

__all__ = ["instagram_facebook_accounts", "instagram_facebook_callback", "instagram_facebook_connect"]

PENDING_SESSION_KEY = "instagram_facebook_connect"
PENDING_MAX_AGE = 15 * 60

OAUTH_FAILED = (
    "Facebook did not complete that connection. Start again from this page, and make sure you accept "
    "every permission it asks for and pick the page your Instagram account is linked to."
)

SUBSCRIBE_FAILED = (
    "That account connected, but Facebook would not start sending its messages here. The app needs "
    "pages_manage_metadata on the linked page; see docs/channels/instagram.md."
)


def _connect_url(workspace_id: str) -> str:
    return reverse("channels:instagram_connect", kwargs={"workspace_id": workspace_id})


@login_required
@require_permission("manage_channels")
@require_POST
def instagram_facebook_connect(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Send the operator to Facebook Login for Business."""
    _clear_pending(request)
    credentials = _app_credentials(request.workspace)
    if not credentials:
        messages.error(request, NOT_CONFIGURED)
        return redirect(_connect_url(workspace_id))
    state = messenger_oauth.mint_state(request.workspace.pk, purpose=instagram_facebook.STATE_PURPOSE)
    return redirect(instagram_facebook.authorize_url(client_id=credentials["client_id"], state=state))


@login_required
@require_http_methods(["GET"])
def instagram_facebook_callback(request: HttpRequest) -> HttpResponse:
    """Where Facebook returns. State, then membership, then permission — see
    ``views_messenger.messenger_oauth_callback`` for why in that order."""
    workspace_id = messenger_oauth.read_state(request.GET.get("state", ""), purpose=instagram_facebook.STATE_PURPOSE)
    if not workspace_id:
        logger.info("Instagram (Facebook Login) connect: a callback arrived with an unusable state.")
        raise Http404("No such connection attempt.")

    membership = _membership_or_404(request.user, workspace_id)
    if not membership.effective_permissions.get("manage_channels", False):
        raise PermissionDenied("Permission denied: manage_channels")
    workspace = membership.workspace
    connect_url = _connect_url(str(workspace.pk))

    code = request.GET.get("code", "")
    if not code or request.GET.get("error"):
        messages.error(request, OAUTH_FAILED)
        return redirect(connect_url)

    credentials = _app_credentials(workspace)
    if not credentials:
        messages.error(request, NOT_CONFIGURED)
        return redirect(connect_url)

    try:
        user_token = instagram_facebook.exchange_code(
            code=code, client_id=credentials["client_id"], client_secret=credentials["client_secret"]
        )
    except APIError:
        logger.info("Instagram (Facebook Login) connect: the code exchange was refused for workspace %s.", workspace.pk)
        messages.error(request, OAUTH_FAILED)
        return redirect(connect_url)

    request.session[PENDING_SESSION_KEY] = {
        "workspace": str(workspace.pk),
        "token": encrypt_value(user_token),
        "at": timezone.now().timestamp(),
    }
    return redirect(reverse("channels:instagram_facebook_accounts", kwargs={"workspace_id": str(workspace.pk)}))


@login_required
@require_permission("manage_channels")
@require_http_methods(["GET", "POST"])
def instagram_facebook_accounts(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """List the Instagram accounts linked to the granted pages; connect the chosen one."""
    token = _pending_token(request, workspace_id)
    if not token:
        messages.error(request, OAUTH_FAILED)
        return redirect(_connect_url(workspace_id))

    try:
        accounts = instagram_facebook.list_accounts(token)
    except APIError:
        logger.info(
            "Instagram (Facebook Login) connect: listing accounts failed for workspace %s.", request.workspace.pk
        )
        _clear_pending(request)
        messages.error(request, OAUTH_FAILED)
        return redirect(_connect_url(workspace_id))

    error = ""
    if request.method == "POST":
        wanted = (request.POST.get("account_id") or "").strip()
        chosen = next((account for account in accounts if account.id == wanted), None)
        if chosen is None:
            error = "Pick one of the accounts below."
        else:
            error = _connect(request, chosen)
            if not error:
                _clear_pending(request)
                return redirect(reverse("channels:list", kwargs={"workspace_id": workspace_id}))

    return render(
        request,
        "channels/instagram_facebook_accounts.html",
        {
            # Ids, usernames and page names only — never a token.
            "accounts": [
                {"id": account.id, "username": account.username, "page_name": account.page_name} for account in accounts
            ],
            "error": error,
            "connect_url": _connect_url(workspace_id),
            "list_url": reverse("channels:list", kwargs={"workspace_id": workspace_id}),
        },
    )


def _connect(request: WorkspaceRequest, account: instagram_facebook.InstagramAccount) -> str:
    """Write the connection, then install the app on the page. "" on success.

    A connection for the same account in this workspace — one made through
    Instagram Login, or an earlier Facebook Login attempt — is switched over in
    place rather than refused, so its conversations and triggers stay attached.
    """
    refusal = plan_refusal(request.workspace)
    if refusal:
        return refusal

    existing = (
        ChannelConnection.objects.for_workspace(request.workspace)
        .filter(platform=Platform.INSTAGRAM.value, external_id=account.id)
        .first()
    )
    connection = existing or ChannelConnection(
        workspace=request.workspace,
        platform=Platform.INSTAGRAM.value,
        external_id=account.id,
    )
    connection.display_name = f"@{account.username}"[:200]
    connection.status = ConnectionStatus.ACTIVE
    connection.credentials = instagram_facebook.credentials_for(account)  # type: ignore[assignment]
    try:
        with transaction.atomic():
            connection.save()
    except IntegrityError:
        # Another workspace holds this account. The wording never says which.
        return DUPLICATE_ACCOUNT_ERROR

    try:
        instagram_facebook.subscribe_page(account)
    except APIError:
        logger.info("Instagram (Facebook Login) connect: subscribing page failed for connection %s.", connection.pk)
        if existing is None:
            connection.delete()
        return SUBSCRIBE_FAILED

    messages.success(request, f"Connected {connection.display_name} through Facebook.")
    return ""


def _pending_token(request: HttpRequest, workspace_id: str) -> str:
    pending = request.session.get(PENDING_SESSION_KEY)
    if not isinstance(pending, dict) or pending.get("workspace") != str(workspace_id):
        return ""
    started = pending.get("at")
    if not isinstance(started, (int, float)) or timezone.now().timestamp() - started > PENDING_MAX_AGE:
        _clear_pending(request)
        return ""
    token = pending.get("token")
    if not isinstance(token, str) or not token:
        return ""
    try:
        return decrypt_value(token)
    except ValueError:
        _clear_pending(request)
        return ""


def _clear_pending(request: HttpRequest) -> None:
    if request.session.pop(PENDING_SESSION_KEY, None) is not None:
        request.session.modified = True
