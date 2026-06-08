"""Seed the CMS with a gateway's theme + chrome + homepage content.

Imports a legacy Airavata Django portal CMS fixture (Wagtail 2.x
``django_airavata_wagtail_base.*`` dump) into this standalone Wagtail 7 CMS.
Rather than ``loaddata`` -- which breaks across the ``use_json_field``
StreamField boundary and the ``PageRevision``/``Page`` schema changes -- it
recreates the portable content through the ORM so Wagtail serializes it for the
installed version:

* ``wagtailimages.image`` -- copied into media storage, pks preserved so the
  snippet/page foreign keys resolve. Renditions regenerate on demand.
* the chrome/theme snippets -- ``CustomCss`` (the gateway theme), ``Navbar``,
  ``FooterText``, ``Announcements``, ``CustomHeaderLinks``.
* the ``HomePage`` content -- mapped onto the existing site-root home page.

Page-tree content (BlankPage/CybergatewayHomePage records, page revisions) is
intentionally skipped; secondary content pages are out of scope for the seed.

Usage::

    python manage.py load_cms_data seagrid
    python manage.py load_cms_data seagrid --images-dir home/fixtures/cms_images/seagrid
"""

import json
import os

from django.conf import settings
from django.core.files.images import ImageFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from wagtail.images import get_image_model
from wagtail.models import Collection

from home.models import (
    Announcements,
    CustomCss,
    CustomHeaderLinks,
    FooterText,
    HomePage,
    Navbar,
)

Image = get_image_model()

# Snippet/page models keyed by the fixture model-name suffix (the app label
# differs between the legacy portal and this project, so match on the suffix).
SNIPPET_FIELDS = {
    "navbar": [
        "logo_redirect_link", "logo_width", "logo_height",
        "logo_text", "logo_text_color", "logo_text_size",
    ],
    "customheaderlinks": [
        "header_link_text", "header_link", "body",
        "header_sub_link_text1", "header_sub_link_text2",
        "header_sub_link_text3", "header_sub_link_text4",
        "header_sub_link1", "header_sub_link2",
        "header_sub_link3", "header_sub_link4",
    ],
}

# HomePage text/choice fields copied verbatim; StreamField + image FKs handled
# separately below.
HOMEPAGE_TEXT_FIELDS = [
    "hero_text", "hero_cta", "features_text",
    "feature_1_title", "feature_1_text",
    "feature_2_title", "feature_2_text",
    "feature_3_title", "feature_3_text",
    "feature_4_title", "feature_4_text",
    "custom_body_message", "show_navbar", "show_nav_extra", "show_footer",
]
HOMEPAGE_IMAGE_FIELDS = [
    "image", "site_logo", "banner_image",
    "feature_logo_1", "feature_logo_2", "feature_logo_3", "feature_logo_4",
]


def _suffix(model_label):
    return model_label.split(".", 1)[1]


class Command(BaseCommand):
    help = "Seed the CMS theme/chrome/homepage from a legacy Airavata CMS fixture."

    def add_arguments(self, parser):
        parser.add_argument(
            "name",
            help="Fixture name under home/fixtures/ (with or without .json).",
        )
        parser.add_argument(
            "--images-dir",
            default=None,
            help="Directory of original image binaries (defaults to "
                 "home/fixtures/cms_images/<name>/).",
        )

    def handle(self, *args, **options):
        fixtures_dir = os.path.join(settings.BASE_DIR, "home", "fixtures")
        name = options["name"][:-5] if options["name"].endswith(".json") else options["name"]
        fixture_file = os.path.join(fixtures_dir, f"{name}.json")
        if not os.path.exists(fixture_file):
            raise CommandError(f"Fixture not found: {fixture_file}")
        images_dir = options["images_dir"] or os.path.join(
            fixtures_dir, "cms_images", name)

        with open(fixture_file) as fh:
            objects = json.load(fh)

        grouped = {}
        for obj in objects:
            grouped.setdefault(_suffix(obj["model"]), []).append(obj)

        with transaction.atomic():
            self._reset()
            id_map = self._load_images(grouped.get("image", []), images_dir)
            self._load_custom_css(grouped.get("customcss", []))
            self._load_navbar(grouped.get("navbar", []), id_map)
            self._load_footer_text(grouped.get("footertext", []))
            self._load_announcements(grouped.get("announcements", []))
            self._load_header_links(grouped.get("customheaderlinks", []))
            self._load_homepage(grouped.get("homepage", []), id_map)

        skipped = sorted(set(grouped) - {
            "image", "customcss", "navbar", "footertext",
            "announcements", "customheaderlinks", "homepage",
        })
        if skipped:
            self.stdout.write(
                "Skipped (not part of the theme seed): " + ", ".join(skipped))
        self.stdout.write(self.style.SUCCESS(f"Loaded CMS data from {name}."))

    # -- helpers ------------------------------------------------------------

    def _reset(self):
        for model in (CustomCss, Navbar, FooterText, Announcements,
                      CustomHeaderLinks):
            model.objects.all().delete()
        Image.objects.all().delete()

    def _load_images(self, image_objects, images_dir):
        """Recreate images with their fixture pks; return {fixture_pk: pk}."""
        root_collection = Collection.get_first_root_node()
        id_map = {}
        for obj in image_objects:
            pk = obj["pk"]
            fields = obj["fields"]
            basename = os.path.basename(fields["file"])
            source = os.path.join(images_dir, basename)
            if not os.path.exists(source):
                self.stdout.write(self.style.WARNING(
                    f"  image binary missing, skipping: {basename}"))
                continue
            image = Image(
                pk=pk,
                title=fields.get("title") or basename,
                width=fields.get("width") or 0,
                height=fields.get("height") or 0,
                collection=root_collection,
            )
            with open(source, "rb") as binary:
                image.file.save(basename, ImageFile(binary), save=False)
            if fields.get("file_size"):
                image.file_size = fields["file_size"]
            # Backfill dimensions from the file if the fixture lacked them.
            if not image.width or not image.height:
                image.width, image.height = image.file.width, image.file.height
            image.save()
            id_map[pk] = image.pk
        self.stdout.write(f"  imported {len(id_map)} images")
        return id_map

    def _load_custom_css(self, objects):
        for obj in objects:
            CustomCss.objects.create(css=json.loads(obj["fields"]["css"]))

    def _load_navbar(self, objects, id_map):
        for obj in objects:
            fields = obj["fields"]
            navbar = Navbar(**{f: fields.get(f) for f in SNIPPET_FIELDS["navbar"]})
            logo_pk = fields.get("logo")
            if logo_pk in id_map:
                navbar.logo_id = id_map[logo_pk]
            navbar.save()

    def _load_footer_text(self, objects):
        for obj in objects:
            FooterText.objects.create(footer=json.loads(obj["fields"]["footer"]))

    def _load_announcements(self, objects):
        for obj in objects:
            fields = obj["fields"]
            Announcements.objects.create(
                announcement_text=fields["announcement_text"],
                announcement_link=fields["announcement_link"],
            )

    def _load_header_links(self, objects):
        for obj in objects:
            fields = obj["fields"]
            CustomHeaderLinks.objects.create(
                **{f: fields.get(f) for f in SNIPPET_FIELDS["customheaderlinks"]})

    def _load_homepage(self, objects, id_map):
        if not objects:
            return
        fields = objects[0]["fields"]
        home = HomePage.objects.filter(slug="home", depth=2).first()
        if home is None:
            self.stdout.write(self.style.WARNING(
                "  no root HomePage found; skipping homepage content"))
            return
        for field in HOMEPAGE_TEXT_FIELDS:
            if field in fields and fields[field] is not None:
                setattr(home, field, fields[field])
        if fields.get("body"):
            home.body = json.loads(fields["body"])
        for field in HOMEPAGE_IMAGE_FIELDS:
            pk = fields.get(field)
            if pk in id_map:
                setattr(home, f"{field}_id", id_map[pk])
        home.save()
        home.save_revision().publish()
        self.stdout.write("  populated root HomePage")
