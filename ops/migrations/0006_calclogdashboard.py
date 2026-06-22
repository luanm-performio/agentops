from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ops", "0005_lockmonitoringrecord_contention_signals_and_more")]

    operations = [
        migrations.CreateModel(
            name="CalcLogDashboard",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=150, unique=True)),
                ("source_folder", models.TextField()),
                ("source_files", models.JSONField(default=list)),
                ("html_content", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-created_at"]},
        )
    ]
