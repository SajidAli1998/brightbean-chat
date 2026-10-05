"""The workspace logo: an uploaded image, normalised to one small square PNG.

Every upload is decoded and re-encoded rather than stored as sent. That is what
makes it safe to serve inline: whatever arrived — a polyglot, a file carrying
EXIF location data, an SVG renamed .png — what leaves is pixels Pillow drew,
in a format Pillow wrote. SVG is refused outright because Pillow cannot read
it, which is the point: an SVG is a document that can carry script.

The pixel count is checked from the header before anything is decoded, for the
same reason apps/media_library/thumbnails.py does it: a few kilobytes of PNG can
declare dimensions that cost gigabytes to expand.
"""

import io
import uuid
from typing import Any

from django.conf import settings
from django.core.files.base import ContentFile

#: Square edge of the stored logo. Large enough for a retina favicon and the
#: 30px sidebar mark at 3x, small enough that it never needs a thumbnail.
LOGO_SIZE = 256
#: An upload over this is refused before it is decoded.
LOGO_MAX_BYTES = 2 * 1024 * 1024
#: What Pillow may decode. GIF keeps its first frame.
ACCEPTED_FORMATS = frozenset({"PNG", "JPEG", "WEBP", "GIF"})
#: For the file input's `accept` attribute.
ACCEPT_ATTRIBUTE = "image/png,image/jpeg,image/webp,image/gif"


class LogoError(ValueError):
    """The upload is not an image this module will store. The message is shown."""


def process_logo(upload: Any) -> ContentFile:
    """Decode ``upload`` and return it as a ``LOGO_SIZE`` square PNG.

    The image is scaled to fit and centred on a transparent square rather than
    cropped, because a logo is usually a wordmark and cropping one cuts its
    text off.
    """
    if upload.size > LOGO_MAX_BYTES:
        raise LogoError("The logo must be 2 MB or smaller.")

    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        upload.seek(0)
        image: Any = Image.open(upload)
        if image.format not in ACCEPTED_FORMATS:
            raise LogoError("Upload a PNG, JPEG, WebP or GIF image.")
        width, height = image.size
        if width * height > int(settings.MEDIA_MAX_IMAGE_PIXELS):
            raise LogoError("That image has too many pixels to use as a logo. Try a smaller one.")
        image.load()
    except LogoError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise LogoError("Upload a PNG, JPEG, WebP or GIF image.") from exc

    image = ImageOps.exif_transpose(image).convert("RGBA")
    image = ImageOps.contain(image, (LOGO_SIZE, LOGO_SIZE), Image.Resampling.LANCZOS)
    square = Image.new("RGBA", (LOGO_SIZE, LOGO_SIZE), (0, 0, 0, 0))
    square.paste(image, ((LOGO_SIZE - image.width) // 2, (LOGO_SIZE - image.height) // 2))

    out = io.BytesIO()
    square.save(out, format="PNG", optimize=True)
    return ContentFile(out.getvalue())


def logo_upload_to(instance: Any, filename: str) -> str:
    """A fresh name per upload, so the URL changes and caches cannot go stale."""
    return f"workspace-logos/{instance.pk}/{uuid.uuid4().hex}.png"


def set_logo(workspace: Any, content: ContentFile) -> None:
    """Store ``content`` (from :func:`process_logo`) as the logo, replacing any old one."""
    previous = workspace.logo.name
    workspace.logo.save("logo.png", content, save=False)
    workspace.save(update_fields=["logo", "updated_at"])
    _delete_file(workspace.logo.storage, previous)


def clear_logo(workspace: Any) -> None:
    """Remove the workspace's logo and its file. A no-op when there is none."""
    previous = workspace.logo.name
    if not previous:
        return
    workspace.logo = ""
    workspace.save(update_fields=["logo", "updated_at"])
    _delete_file(workspace.logo.storage, previous)


def _delete_file(storage: Any, name: str) -> None:
    # After the row points elsewhere, so a failed delete leaves an orphaned
    # file rather than a row pointing at nothing.
    if name:
        storage.delete(name)
