"""The workspace logo: upload, normalisation, serving, and where it shows."""

import io
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.members.models import OrgMembership
from apps.members.roles import OrgRole
from apps.workspaces.logo import LOGO_SIZE, LogoError, process_logo
from tests.support import create_user


def _image(fmt: str = "PNG", size: tuple[int, int] = (400, 200), name: str = "logo.png") -> SimpleUploadedFile:
    out = io.BytesIO()
    Image.new("RGB", size, (78, 90, 183)).save(out, format=fmt)
    return SimpleUploadedFile(name, out.getvalue(), content_type="image/png")


@pytest.fixture(autouse=True)
def _media_root(settings: Any, tmp_path: Any) -> None:
    settings.MEDIA_ROOT = tmp_path


def _upload(client: Any, workspace: Any, **extra: Any) -> Any:
    data = {"name": workspace.name, **extra}
    return client.post(f"/w/{workspace.pk}/settings/update/", data, follow=True)


class TestProcessLogo:
    def test_any_accepted_image_becomes_a_square_png(self):
        stored = Image.open(io.BytesIO(process_logo(_image("JPEG", name="logo.jpg")).read()))

        assert stored.format == "PNG"
        assert stored.size == (LOGO_SIZE, LOGO_SIZE)

    def test_a_wide_image_is_padded_not_cropped(self):
        stored = Image.open(io.BytesIO(process_logo(_image(size=(400, 100))).read()))

        assert stored.getpixel((0, 0))[3] == 0  # transparent padding
        assert stored.getpixel((LOGO_SIZE // 2, LOGO_SIZE // 2))[3] == 255

    def test_svg_is_refused(self):
        svg = SimpleUploadedFile("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>')

        with pytest.raises(LogoError):
            process_logo(svg)

    def test_an_oversized_upload_is_refused_before_decoding(self):
        big = SimpleUploadedFile("logo.png", b"\0" * (2 * 1024 * 1024 + 1))

        with pytest.raises(LogoError, match="2 MB"):
            process_logo(big)

    def test_too_many_pixels_is_refused(self, settings):
        settings.MEDIA_MAX_IMAGE_PIXELS = 100

        with pytest.raises(LogoError):
            process_logo(_image(size=(20, 20)))


@pytest.mark.django_db
class TestUploading:
    def test_uploading_stores_the_logo(self, tenancy, client_for):
        _upload(client_for(tenancy.owner), tenancy.workspace, logo=_image())

        tenancy.workspace.refresh_from_db()
        assert tenancy.workspace.logo.name.startswith(f"workspace-logos/{tenancy.workspace.pk}/")

    def test_a_bad_file_is_refused_and_nothing_else_is_saved(self, tenancy, client_for):
        bad = SimpleUploadedFile("logo.png", b"not an image")

        response = _upload(client_for(tenancy.owner), tenancy.workspace, logo=bad, description="changed")

        tenancy.workspace.refresh_from_db()
        assert not tenancy.workspace.logo
        assert tenancy.workspace.description != "changed"
        assert b"Upload a PNG, JPEG, WebP or GIF image." in response.content

    def test_replacing_deletes_the_old_file(self, tenancy, client_for):
        client = client_for(tenancy.owner)
        _upload(client, tenancy.workspace, logo=_image())
        tenancy.workspace.refresh_from_db()
        first = tenancy.workspace.logo.name

        _upload(client, tenancy.workspace, logo=_image())

        tenancy.workspace.refresh_from_db()
        assert tenancy.workspace.logo.name != first
        assert not tenancy.workspace.logo.storage.exists(first)

    def test_remove_clears_the_logo_and_its_file_at_once(self, tenancy, client_for):
        client = client_for(tenancy.owner)
        _upload(client, tenancy.workspace, logo=_image())
        tenancy.workspace.refresh_from_db()
        name = tenancy.workspace.logo.name

        response = client.post(f"/w/{tenancy.workspace.pk}/settings/logo/remove/", follow=True)

        tenancy.workspace.refresh_from_db()
        assert not tenancy.workspace.logo
        assert not tenancy.workspace.logo.storage.exists(name)
        assert b"Logo removed." in response.content

    def test_a_member_without_the_permission_cannot_remove(self, tenancy, client_for):
        _upload(client_for(tenancy.owner), tenancy.workspace, logo=_image())

        client_for(tenancy.user_for("viewer")).post(f"/w/{tenancy.workspace.pk}/settings/logo/remove/")

        tenancy.workspace.refresh_from_db()
        assert tenancy.workspace.logo

    def test_saving_without_a_file_keeps_the_logo(self, tenancy, client_for):
        client = client_for(tenancy.owner)
        _upload(client, tenancy.workspace, logo=_image())

        _upload(client, tenancy.workspace, description="changed")

        tenancy.workspace.refresh_from_db()
        assert tenancy.workspace.logo

    def test_a_member_without_the_permission_cannot_upload(self, tenancy, client_for):
        _upload(client_for(tenancy.user_for("viewer")), tenancy.workspace, logo=_image())

        tenancy.workspace.refresh_from_db()
        assert not tenancy.workspace.logo


@pytest.mark.django_db
class TestServing:
    def _with_logo(self, tenancy: Any, client_for: Any) -> str:
        _upload(client_for(tenancy.owner), tenancy.workspace, logo=_image())
        tenancy.workspace.refresh_from_db()
        return tenancy.workspace.logo_url

    def test_a_member_gets_the_png(self, tenancy, client_for):
        url = self._with_logo(tenancy, client_for)

        response = client_for(tenancy.user_for("viewer")).get(url)

        assert response.status_code == 200
        assert response["Content-Type"] == "image/png"
        assert "immutable" in response["Cache-Control"]

    def test_an_org_member_outside_the_workspace_gets_it(self, tenancy, client_for):
        """The org's Workspaces page lists workspaces you are not in."""
        url = self._with_logo(tenancy, client_for)
        outsider = create_user("outsider@example.test")
        OrgMembership.objects.create(user=outsider, organization=tenancy.organization, org_role=OrgRole.MEMBER)

        assert client_for(outsider).get(url).status_code == 200

    def test_an_archived_workspace_still_serves_it(self, tenancy, client_for):
        url = self._with_logo(tenancy, client_for)
        tenancy.workspace.is_archived = True
        tenancy.workspace.save(update_fields=["is_archived"])

        assert client_for(tenancy.owner).get(url).status_code == 200

    def test_another_tenant_gets_a_404(self, tenancy, other_tenancy, client_for):
        url = self._with_logo(tenancy, client_for)

        assert client_for(other_tenancy.owner).get(url).status_code == 404

    def test_a_workspace_without_a_logo_is_a_404(self, tenancy, client_for):
        response = client_for(tenancy.owner).get(f"/workspace-logo/{tenancy.workspace.pk}/")

        assert response.status_code == 404

    def test_fetching_a_logo_does_not_move_the_current_workspace(self, tenancy, client_for):
        """The switcher loads every workspace's logo on every page."""
        url = self._with_logo(tenancy, client_for)
        tenancy.owner.last_workspace_id = None
        tenancy.owner.save(update_fields=["last_workspace_id"])

        client_for(tenancy.owner).get(url)

        tenancy.owner.refresh_from_db()
        assert tenancy.owner.last_workspace_id is None

    def test_a_new_upload_changes_the_url(self, tenancy, client_for):
        first = self._with_logo(tenancy, client_for)

        assert self._with_logo(tenancy, client_for) != first


@pytest.mark.django_db
class TestThePageShell:
    def test_the_logo_is_the_mark_and_the_favicon(self, tenancy, client_for):
        client = client_for(tenancy.owner)
        _upload(client, tenancy.workspace, logo=_image())
        tenancy.workspace.refresh_from_db()

        html = client.get(f"/w/{tenancy.workspace.pk}/").content.decode()

        assert f'<link rel="icon" type="image/png" href="{tenancy.workspace.logo_url}">' in html
        assert "sidebar-logo-mark-image" in html
        assert "favicon/favicon.svg" not in html

    def test_the_organizations_workspace_list_shows_it(self, tenancy, client_for):
        client = client_for(tenancy.owner)
        _upload(client, tenancy.workspace, logo=_image())
        tenancy.workspace.refresh_from_db()

        html = client.get("/organization/workspaces/").content.decode()

        assert f'<img src="{tenancy.workspace.logo_url}"' in html

    def test_without_a_logo_the_brightbean_favicon_stays(self, tenancy, client_for):
        html = client_for(tenancy.owner).get(f"/w/{tenancy.workspace.pk}/").content.decode()

        assert "favicon/favicon.svg" in html

    def test_the_tab_title_names_the_workspace(self, tenancy, client_for):
        tenancy.workspace.name = "Rugby & Co"
        tenancy.workspace.save(update_fields=["name"])

        html = client_for(tenancy.owner).get(f"/w/{tenancy.workspace.pk}/settings/").content.decode()

        assert "<title>Workspace settings · Rugby &amp; Co</title>" in html

    def test_the_login_page_keeps_the_product_name(self, client):
        html = client.get("/accounts/login/").content.decode()

        assert "BrightBean Chat</title>" in html
