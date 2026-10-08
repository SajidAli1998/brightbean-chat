"""Messenger sender profiles: queued on first contact, fetched on the worker."""

from typing import Any

import httpx
import pytest

from apps.channels import messenger_profile
from apps.channels.events import EventPayload, EventType, NormalizedEvent
from apps.channels.models import ChannelConnection
from apps.channels.tests.messenger_support import PAGE_TOKEN, PSID, FakeGraph, Reply, fake_graph
from apps.messaging.identities import resolve_identity
from apps.queueing.models import ScheduledAction

pytestmark = pytest.mark.django_db

PROFILE = {
    "first_name": "Sajid",
    "last_name": "Ali",
    "profile_pic": "https://platform-lookaside.fbsbx.com/platform/profilepic/?psid=222",
    "locale": "en_GB",
    "timezone": 3,
    "id": PSID,
}


def _event(connection: ChannelConnection, *, kind: EventType = EventType.MESSAGE, psid: str = PSID) -> NormalizedEvent:
    from django.utils import timezone

    return NormalizedEvent(
        type=kind,
        connection=connection,
        platform_user_id=psid,
        provider_event_id=f"mid.{kind}.{psid}",
        timestamp=timezone.now(),
        payload=EventPayload(text="hi"),
    )


def _actions(connection: ChannelConnection) -> list[ScheduledAction]:
    return list(ScheduledAction.objects.for_workspace(connection.workspace).filter(type=messenger_profile.ACTION_TYPE))


def _identity(connection: ChannelConnection, psid: str = PSID) -> Any:
    return resolve_identity(connection, psid).identity


def _run(connection: ChannelConnection) -> None:
    """Queue for the fixture's sender and run the row the way the worker would.

    The identity is resolved first because that is persistence's job, and
    persistence runs before this late stage on the real seam.
    """
    _identity(connection)
    messenger_profile.profile_events(connection, [_event(connection)])
    (action,) = _actions(connection)
    messenger_profile.fetch_profile(action.payload, action)


class TestEtcZone:
    @pytest.mark.parametrize(
        ("offset", "zone"),
        [
            (3, "Etc/GMT-3"),
            (-5, "Etc/GMT+5"),
            (0, "Etc/GMT"),
            (14, "Etc/GMT-14"),
            (-12, "Etc/GMT+12"),
            (3.0, "Etc/GMT-3"),
        ],
    )
    def test_whole_hours_become_posix_signed_zones(self, offset: Any, zone: str) -> None:
        assert messenger_profile.etc_zone(offset) == zone

    @pytest.mark.parametrize("offset", [5.5, 15, -13, "3", None, True])
    def test_anything_without_an_etc_zone_is_left_out(self, offset: Any) -> None:
        assert messenger_profile.etc_zone(offset) == ""

    def test_every_zone_it_produces_is_one_zoneinfo_accepts(self) -> None:
        from zoneinfo import ZoneInfo

        for hours in range(-12, 15):
            ZoneInfo(messenger_profile.etc_zone(hours))


class TestQueueing:
    def test_a_new_sender_queues_one_lookup(self, page: ChannelConnection) -> None:
        identity = _identity(page)
        messenger_profile.profile_events(page, [_event(page), _event(page, kind=EventType.POSTBACK)])
        (action,) = _actions(page)
        assert action.payload == {"identity_id": str(identity.pk)}
        assert action.contact_id == identity.contact_id

    def test_a_second_delivery_the_same_day_queues_nothing_more(self, page: ChannelConnection) -> None:
        _identity(page)
        messenger_profile.profile_events(page, [_event(page)])
        messenger_profile.profile_events(page, [_event(page)])
        assert len(_actions(page)) == 1

    def test_a_sender_already_looked_up_is_left_alone(self, page: ChannelConnection) -> None:
        identity = _identity(page)
        identity.extra = {messenger_profile.FETCHED_KEY: "2026-10-08T09:42:00+00:00"}
        identity.save()
        messenger_profile.profile_events(page, [_event(page)])
        assert _actions(page) == []

    def test_a_comment_author_is_not_looked_up(self, page: ChannelConnection) -> None:
        """A comment's author id is not a PSID; the User Profile API cannot answer for it."""
        _identity(page)
        messenger_profile.profile_events(page, [_event(page, kind=EventType.COMMENT)])
        assert _actions(page) == []

    def test_other_platforms_are_ignored(self, connection: ChannelConnection) -> None:
        _identity(connection)
        messenger_profile.profile_events(connection, [_event(connection)])
        assert _actions(connection) == []

    def test_a_failure_to_queue_never_fails_the_batch(self, page: ChannelConnection, monkeypatch: Any) -> None:
        def explode(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("queue down")

        monkeypatch.setattr(messenger_profile, "_queue_lookups", explode)
        messenger_profile.profile_events(page, [_event(page)])


class TestFetching:
    def test_a_profile_fills_the_contacts_blank_fields(self, page: ChannelConnection) -> None:
        with fake_graph(lambda graph: graph.reply(f"/{PSID}", Reply(body=PROFILE))):
            _run(page)
        identity = _identity(page)
        contact = identity.contact
        contact.refresh_from_db()
        assert (contact.first_name, contact.last_name) == ("Sajid", "Ali")
        assert contact.locale == "en_GB"
        assert contact.timezone == "Etc/GMT-3"
        assert contact.display_name == "Sajid Ali"

    def test_the_identity_keeps_the_photo_and_is_marked_fetched(self, page: ChannelConnection) -> None:
        with fake_graph(lambda graph: graph.reply(f"/{PSID}", Reply(body=PROFILE))):
            _run(page)
        identity = _identity(page)
        identity.refresh_from_db()
        assert identity.extra["profile_pic_url"] == PROFILE["profile_pic"]
        assert identity.extra["first_name"] == "Sajid"
        assert identity.extra[messenger_profile.FETCHED_KEY]

    def test_it_asks_for_every_field_with_the_token_in_a_header(self, page: ChannelConnection) -> None:
        with fake_graph(lambda graph: graph.reply(f"/{PSID}", Reply(body=PROFILE))) as graph:
            _run(page)
        (call,) = graph.calls
        assert call.method == "GET"
        assert call.path == f"/v21.0/{PSID}"
        assert call.params == {"fields": messenger_profile.FULL_FIELDS}
        assert call.authorization == f"Bearer {PAGE_TOKEN}"

    def test_a_name_somebody_already_set_is_never_overwritten(self, page: ChannelConnection) -> None:
        contact = _identity(page).contact
        contact.first_name = "Captain"
        contact.save()
        with fake_graph(lambda graph: graph.reply(f"/{PSID}", Reply(body=PROFILE))):
            _run(page)
        contact.refresh_from_db()
        assert contact.first_name == "Captain"
        assert contact.last_name == "Ali"

    def test_a_missing_locale_permission_falls_back_to_name_and_photo(self, page: ChannelConnection) -> None:
        def configure(graph: FakeGraph) -> None:
            def handle(request: httpx.Request) -> httpx.Response:
                graph.calls.append(
                    messenger_support_call(request)  # recorded so the test can count the attempts
                )
                if request.url.params.get("fields") == messenger_profile.FULL_FIELDS:
                    return httpx.Response(
                        400, json={"error": {"message": "(#10) Requires pages_user_locale", "code": 10}}
                    )
                return httpx.Response(200, json={"first_name": "Sajid", "last_name": "Ali", "id": PSID})

            graph.handle = handle  # type: ignore[method-assign]

        with fake_graph(configure) as graph:
            _run(page)
        assert [call.params["fields"] for call in graph.calls] == [
            messenger_profile.FULL_FIELDS,
            messenger_profile.BASIC_FIELDS,
        ]
        contact = _identity(page).contact
        contact.refresh_from_db()
        assert contact.display_name == "Sajid Ali"
        assert contact.locale == ""

    def test_a_refusal_finishes_the_row_without_writing(self, page: ChannelConnection) -> None:
        with fake_graph(lambda graph: graph.reply(f"/{PSID}", Reply(status=400))):
            _run(page)
        identity = _identity(page)
        identity.refresh_from_db()
        assert messenger_profile.FETCHED_KEY not in identity.extra
        assert identity.contact.first_name == ""

    @pytest.mark.parametrize("status", [429, 500])
    def test_a_throttle_or_outage_raises_so_the_queue_retries(self, page: ChannelConnection, status: int) -> None:
        from apps.channels.providers.exceptions import APIError

        with fake_graph(lambda graph: graph.reply(f"/{PSID}", Reply(status=status))), pytest.raises(APIError):
            _run(page)

    def test_a_psid_that_is_not_digits_is_never_put_in_a_path(self, page: ChannelConnection) -> None:
        _identity(page, psid="../me/accounts")
        messenger_profile.profile_events(page, [_event(page, psid="../me/accounts")])
        (action,) = _actions(page)
        with fake_graph() as graph:
            messenger_profile.fetch_profile(action.payload, action)
        assert graph.calls == []

    def test_a_photo_url_that_is_not_https_is_dropped(self, page: ChannelConnection) -> None:
        body = {**PROFILE, "profile_pic": "javascript:alert(1)"}
        with fake_graph(lambda graph: graph.reply(f"/{PSID}", Reply(body=body))):
            _run(page)
        identity = _identity(page)
        identity.refresh_from_db()
        assert "profile_pic_url" not in identity.extra


def messenger_support_call(request: httpx.Request) -> Any:
    from apps.channels.tests.messenger_support import Call

    return Call(
        method=request.method,
        path=request.url.path,
        body={},
        params=dict(request.url.params),
        authorization=request.headers.get("Authorization", ""),
    )
