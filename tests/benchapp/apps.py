from django.apps import AppConfig


class BenchAppConfig(AppConfig):
    name = "tests.benchapp"
    label = "benchapp"
    default_auto_field = "django.db.models.BigAutoField"
