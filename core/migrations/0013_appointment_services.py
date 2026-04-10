from django.db import migrations, models


def copy_service_to_services(apps, schema_editor):
    Appointment = apps.get_model("core", "Appointment")
    through_model = Appointment.services.through
    db_alias = schema_editor.connection.alias

    through_rows = []
    for appointment in Appointment.objects.using(db_alias).exclude(service_id=None).iterator():
        through_rows.append(
            through_model(
                appointment_id=appointment.pk,
                service_id=appointment.service_id,
            )
        )

    if through_rows:
        through_model.objects.using(db_alias).bulk_create(through_rows, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0012_service_description_service_is_active_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="services",
            field=models.ManyToManyField(related_name="appointments_multi", to="core.service"),
        ),
        migrations.RunPython(copy_service_to_services, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="appointment",
            name="service",
        ),
        migrations.AlterField(
            model_name="appointment",
            name="services",
            field=models.ManyToManyField(related_name="appointments", to="core.service"),
        ),
    ]
