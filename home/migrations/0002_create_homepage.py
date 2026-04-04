from django.db import migrations


def create_homepage(apps, schema_editor):
    content_type_model = apps.get_model("contenttypes.ContentType")
    page_model = apps.get_model("wagtailcore.Page")
    site_model = apps.get_model("wagtailcore.Site")
    homepage_model = apps.get_model("home.HomePage")

    page_content_type = content_type_model.objects.get(
        model="page",
        app_label="wagtailcore",
    )
    page_model.objects.filter(
        content_type=page_content_type,
        slug="home",
        depth=2,
    ).delete()

    homepage_content_type, _ = content_type_model.objects.get_or_create(
        model="homepage",
        app_label="home",
    )

    homepage = homepage_model.objects.create(
        title="Home",
        draft_title="Home",
        slug="home",
        content_type=homepage_content_type,
        path="00010001",
        depth=2,
        numchild=0,
        url_path="/home/",
    )

    site_model.objects.update_or_create(
        hostname="localhost",
        defaults={
            "root_page": homepage,
            "is_default_site": True,
            "site_name": "Agenda de Citas",
        },
    )


def remove_homepage(apps, schema_editor):
    content_type_model = apps.get_model("contenttypes.ContentType")
    homepage_model = apps.get_model("home.HomePage")

    homepage_model.objects.filter(slug="home", depth=2).delete()
    content_type_model.objects.filter(model="homepage", app_label="home").delete()


class Migration(migrations.Migration):
    run_before = [
        ("wagtailcore", "0053_locale_model"),
    ]

    dependencies = [
        ("home", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_homepage, remove_homepage),
    ]
