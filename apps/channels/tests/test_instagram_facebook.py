"""Instagram through Facebook Login for Business: the connect flow and the variant switch."""

from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.channels import instagram_facebook, instagram_oauth, messenger_oauth
from apps.channels.events import OutboundMessage, TextBlock
from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.channels.providers import instagram
from apps.channels.providers.instagram import InstagramAdapter
from apps.channels.providers.messenger import SUBSCRIBED_FIELDS
from apps.channels.tests import instagram_support
from apps.channels.tests.instagram_support import IG_ACCOUNT_ID, IG_USER_ID, request_for
from apps.channels.tests.messenger_support import APP_SECRET as FACEBOOK_APP_SECRET
from apps.channels.tests.messenger_support import PAGE_TOKEN, Reply, fake_graph
from apps.channels.views_instagram_facebook import PENDING_SESSION_KEY
from apps.common.platforms import Platform
from apps.members.roles import WorkspaceRole
from tests.form_action import assert_page_permits_its_redirect
from tests.support import Tenancy, create_tenancy

pytestmark = pytest.mark.django_db

CALLBACK = reverse("instagram_facebook_callback")
USER_TOKEN = "EAA" + "userTOKEN0123" * 3  # noqa: S105 - a fake credential for tests
PAGE_ID = "555555555555555"
LINKED_PAGE = {
    "id": PAGE_ID,
    "name": "All Things Rugby",
    "access_token": PAGE_TOKEN,
    "instagram_business_account": {"id": IG_ACCOUNT_ID, "username": "allthingsrugby"},
}
UNLINKED_PAGE = {"id": "666666666666666", "name": "No Instagram", "access_token": PAGE_TOKEN}


def instagram_connect_url(tenancy: Tenancy) -> str:
    return reverse("channels:instagram_connect", kwargs={"workspace_id": tenancy.workspace.pk})


def start_url(tenancy: Tenancy) -> str:
    return reverse("channels:instagram_facebook_connect", kwargs={"workspace_id": tenancy.workspace.pk})


def accounts_url(tenancy: Tenancy) -> str:
    return reverse("channels:instagram_facebook_accounts", kwargs={"workspace_id": tenancy.workspace.pk})


def admin(tenancy: Tenancy, client_for: Any) -> Client:
    return client_for(tenancy.user_for(WorkspaceRole.ADMIN))


def graph_for(pages: list[dict[str, Any]] | None = None) -> Any:
    def configure(graph: Any) -> None:
        graph.reply("/oauth/access_token", Reply(body={"access_token": USER_TOKEN}))
        graph.reply("/me/accounts", Reply(body={"data": pages if pages is not None else [LINKED_PAGE]}))
        graph.reply("/subscribed_apps", Reply(body={"success": True}))

    return configure


def state_for(tenancy: Tenancy) -> str:
    return messenger_oauth.mint_state(tenancy.workspace.pk, purpose=instagram_facebook.STATE_PURPOSE)


def through_callback(client: Client, tenancy: Tenancy) -> Any:
    with fake_graph(graph_for()):
        return client.get(CALLBACK, {"code": "a-real-code", "state": state_for(tenancy)})


def connect(client: Client, tenancy: Tenancy, pages: list[dict[str, Any]] | None = None) -> tuple[Any, Any]:
    through_callback(client, tenancy)
    with fake_graph(graph_for(pages)) as graph:
        response = client.post(accounts_url(tenancy), {"account_id": IG_ACCOUNT_ID})
    return response, graph


@pytest.fixture
def facebook_connection(tenancy: Tenancy, app_secret: str) -> ChannelConnection:
    account = instagram_facebook.InstagramAccount(
        id=IG_ACCOUNT_ID,
        username="allthingsrugby",
        page_id=PAGE_ID,
        page_name="All Things Rugby",
        page_token=PAGE_TOKEN,
    )
    connection = ChannelConnection(
        workspace=tenancy.workspace,
        platform=Platform.INSTAGRAM.value,
        display_name="@allthingsrugby",
        external_id=IG_ACCOUNT_ID,
        status=ConnectionStatus.ACTIVE,
    )
    connection.credentials = instagram_facebook.credentials_for(account)  # type: ignore[assignment]
    connection.save()
    return connection


def hosts_of(fake: Any) -> list[str]:
    return fake.hosts


def recording_hosts(fake: Any) -> None:
    """Make an Instagram ``FakeGraph`` remember which host each call went to."""
    fake.hosts = []
    original = fake.handle

    def handle(request: httpx.Request) -> httpx.Response:
        fake.hosts.append(request.url.host)
        return original(request)

    fake.handle = handle


class TestStarting:
    def test_the_instagram_page_offers_the_facebook_route(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        response = admin(tenancy, client_for).get(instagram_connect_url(tenancy))
        assert response.status_code == 200
        assert start_url(tenancy).encode() in response.content
        assert instagram_facebook.callback_url().encode() in response.content

    def test_it_sends_the_operator_to_facebook_with_instagram_scopes(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        response = admin(tenancy, client_for).post(start_url(tenancy))
        assert response.status_code == 302
        target = urlparse(response["Location"])
        assert target.netloc == "www.facebook.com"
        query = parse_qs(target.query)
        assert set(query["scope"][0].split(",")) == set(instagram_facebook.SCOPES)
        assert query["redirect_uri"] == [instagram_facebook.callback_url()]
        assert query["client_id"] == ["1234567890"]
        assert messenger_oauth.read_state(query["state"][0], purpose=instagram_facebook.STATE_PURPOSE)

    def test_the_redirect_is_one_the_pages_form_action_allows(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        assert_page_permits_its_redirect(
            admin(tenancy, client_for), instagram_connect_url(tenancy), submits_to=start_url(tenancy)
        )

    def test_no_facebook_app_configured_says_so(self, tenancy: Tenancy, client_for: Any) -> None:
        client = admin(tenancy, client_for)
        response = client.post(start_url(tenancy), follow=True)
        assert b"PLATFORM_MESSENGER_CLIENT_ID" in response.content

    @pytest.mark.parametrize("role", [WorkspaceRole.EDITOR, WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_only_channel_managers_can_start(
        self, tenancy: Tenancy, client_for: Any, app_secret: str, role: str
    ) -> None:
        assert client_for(tenancy.user_for(role)).post(start_url(tenancy)).status_code == 403


class TestTheCallback:
    def test_a_messenger_state_is_not_accepted_here(self, tenancy: Tenancy, client_for: Any, app_secret: str) -> None:
        response = admin(tenancy, client_for).get(
            CALLBACK, {"code": "a-real-code", "state": messenger_oauth.mint_state(tenancy.workspace.pk)}
        )
        assert response.status_code == 404

    def test_a_state_for_somebody_elses_workspace_is_a_404(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        rival = create_tenancy("rival-ig")
        response = admin(tenancy, client_for).get(CALLBACK, {"code": "a-real-code", "state": state_for(rival)})
        assert response.status_code == 404

    def test_the_user_token_is_held_encrypted_and_the_chooser_follows(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        client = admin(tenancy, client_for)
        response = through_callback(client, tenancy)
        assert response.status_code == 302
        assert response["Location"] == accounts_url(tenancy)
        pending = client.session[PENDING_SESSION_KEY]
        assert USER_TOKEN not in str(pending)

    def test_the_exchange_uses_this_flows_redirect_uri(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        with fake_graph(graph_for()) as graph:
            admin(tenancy, client_for).get(CALLBACK, {"code": "a-real-code", "state": state_for(tenancy)})
        first = graph.bodies("/oauth/access_token")[0]
        assert first["redirect_uri"] == instagram_facebook.callback_url()


class TestChoosingAnAccount:
    def test_only_pages_with_a_linked_account_are_offered(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        client = admin(tenancy, client_for)
        through_callback(client, tenancy)
        with fake_graph(graph_for([LINKED_PAGE, UNLINKED_PAGE])):
            response = client.get(accounts_url(tenancy))
        assert b"@allthingsrugby" in response.content
        assert b"No Instagram" not in response.content
        assert PAGE_TOKEN.encode() not in response.content

    def test_connecting_stores_the_page_token_as_a_facebook_login_row(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        response, graph = connect(admin(tenancy, client_for), tenancy)
        assert response.status_code == 302
        connection = ChannelConnection.objects.for_workspace(tenancy.workspace).get(platform=Platform.INSTAGRAM)
        assert connection.external_id == IG_ACCOUNT_ID
        assert connection.display_name == "@allthingsrugby"
        assert instagram_oauth.is_facebook_login(connection)
        assert instagram_oauth.access_token(connection) == PAGE_TOKEN
        assert connection.credentials[instagram_oauth.PAGE_ID_KEY] == PAGE_ID  # type: ignore[index]

    def test_connecting_installs_the_app_on_the_page_with_messengers_fields(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        _, graph = connect(admin(tenancy, client_for), tenancy)
        (call,) = [call for call in graph.calls if call.matches(f"/{PAGE_ID}/subscribed_apps")]
        assert call.method == "POST"
        assert call.params["subscribed_fields"] == ",".join(SUBSCRIBED_FIELDS)
        assert call.authorization == f"Bearer {PAGE_TOKEN}"

    def test_a_refused_subscription_leaves_no_connection(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        client = admin(tenancy, client_for)
        through_callback(client, tenancy)

        def configure(graph: Any) -> None:
            graph_for()(graph)
            graph.reply("/subscribed_apps", Reply(status=400))

        with fake_graph(configure):
            response = client.post(accounts_url(tenancy), {"account_id": IG_ACCOUNT_ID})
        assert response.status_code == 200
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()

    def test_an_existing_instagram_login_row_is_switched_over_in_place(
        self, tenancy: Tenancy, client_for: Any, app_secret: str, instagram_connection: ChannelConnection
    ) -> None:
        connect(admin(tenancy, client_for), tenancy)
        instagram_connection.refresh_from_db()
        assert instagram_oauth.is_facebook_login(instagram_connection)
        assert ChannelConnection.objects.for_workspace(tenancy.workspace).count() == 1

    def test_an_account_another_workspace_holds_is_refused(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        rival = create_tenancy("rival-holds")
        ChannelConnection.objects.create(
            workspace=rival.workspace, platform=Platform.INSTAGRAM.value, display_name="@x", external_id=IG_ACCOUNT_ID
        )
        response, _ = connect(admin(tenancy, client_for), tenancy)
        assert response.status_code == 200
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()

    def test_a_crafted_account_id_is_refused(self, tenancy: Tenancy, client_for: Any, app_secret: str) -> None:
        client = admin(tenancy, client_for)
        through_callback(client, tenancy)
        with fake_graph(graph_for()):
            response = client.post(accounts_url(tenancy), {"account_id": "17841499999999999"})
        assert response.status_code == 200
        assert b"Pick one of the accounts" in response.content

    def test_without_a_pending_attempt_it_starts_over(self, tenancy: Tenancy, client_for: Any, app_secret: str) -> None:
        response = admin(tenancy, client_for).get(accounts_url(tenancy))
        assert response.status_code == 302
        assert response["Location"] == instagram_connect_url(tenancy)

    def test_a_stale_attempt_is_dropped(self, tenancy: Tenancy, client_for: Any, app_secret: str) -> None:
        client = admin(tenancy, client_for)
        through_callback(client, tenancy)
        session = client.session
        session[PENDING_SESSION_KEY]["at"] = timezone.now().timestamp() - 3600
        session.save()
        assert client.get(accounts_url(tenancy)).status_code == 302


class TestTheVariantSwitch:
    def test_sends_go_to_graph_facebook_with_the_page_token(self, facebook_connection: ChannelConnection) -> None:
        with instagram_support.fake_graph(recording_hosts) as graph:
            InstagramAdapter().send(
                facebook_connection,
                SimpleNamespace(platform_user_id=IG_USER_ID, last_inbound_at="2026-08-01T00:00:00Z", contact=None),
                OutboundMessage(blocks=(TextBlock(text="Hi"),)),
            )
        assert graph.paths() == ["me/messages"]
        assert hosts_of(graph) == ["graph.facebook.com"]
        assert graph.tokens == [f"Bearer {PAGE_TOKEN}"]

    def test_an_instagram_login_row_still_goes_to_graph_instagram(
        self, instagram_connection: ChannelConnection
    ) -> None:
        with instagram_support.fake_graph(recording_hosts) as graph:
            InstagramAdapter().send(
                instagram_connection,
                SimpleNamespace(platform_user_id=IG_USER_ID, last_inbound_at="2026-08-01T00:00:00Z", contact=None),
                OutboundMessage(blocks=(TextBlock(text="Hi"),)),
            )
        assert hosts_of(graph) == ["graph.instagram.com"]

    def test_recent_media_reads_the_accounts_own_edge(self, facebook_connection: ChannelConnection) -> None:
        with instagram_support.fake_graph(recording_hosts) as graph:
            instagram.recent_media(facebook_connection)
        assert graph.paths() == [f"{IG_ACCOUNT_ID}/media"]
        assert hosts_of(graph) == ["graph.facebook.com"]

    @pytest.mark.usefixtures("both_meta_apps")
    def test_deliveries_are_verified_with_the_facebook_app_secret(self, facebook_connection: ChannelConnection) -> None:
        payload = {"object": "instagram", "entry": [{"id": IG_ACCOUNT_ID, "time": 1, "messaging": []}]}
        adapter = InstagramAdapter()
        assert adapter.verify_webhook(request_for(payload, secret=FACEBOOK_APP_SECRET), facebook_connection)
        assert not adapter.verify_webhook(request_for(payload), facebook_connection)

    @pytest.mark.usefixtures("both_meta_apps")
    def test_instagram_login_rows_keep_the_instagram_app_secret(self, instagram_connection: ChannelConnection) -> None:
        payload = {"object": "instagram", "entry": [{"id": IG_ACCOUNT_ID, "time": 1, "messaging": []}]}
        adapter = InstagramAdapter()
        assert adapter.verify_webhook(request_for(payload), instagram_connection)
        assert not adapter.verify_webhook(request_for(payload, secret=FACEBOOK_APP_SECRET), instagram_connection)

    def test_the_token_refresh_sweep_leaves_page_tokens_alone(self, facebook_connection: ChannelConnection) -> None:
        with instagram_support.fake_graph() as graph:
            refreshed = instagram_oauth.refresh_expiring_tokens()
        assert refreshed == 0
        assert graph.calls == []
        facebook_connection.refresh_from_db()
        assert facebook_connection.status == ConnectionStatus.ACTIVE
