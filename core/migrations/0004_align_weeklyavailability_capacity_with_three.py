import django.core.validators
from django.db import migrations, models


def set_capacity_three_for_existing_default_slots(apps, schema_editor):
    WeeklyAvailability = apps.get_model("core", "WeeklyAvailability")
    WeeklyAvailability.objects.filter(capacity=2).update(capacity=3)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0003_weeklyavailability_capacity"),
    ]

    operations = [
        migrations.AlterField(
            model_name="weeklyavailability",
            name="capacity",
            field=models.PositiveSmallIntegerField(
                default=3,
                validators=[django.core.validators.MinValueValidator(1)],
            ),
        ),
        migrations.RunPython(
            set_capacity_three_for_existing_default_slots,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
