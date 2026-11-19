"""Built at import from the policy skeleton: one stand-in class per model the policy touches."""

from django.conf import settings

from tests.bench import standins

SKELETON, POLICY = standins.load(settings.REBAC_BENCH_POLICY)
# The stand-ins by the label of the model they stand for (``"messaging.Message"``).
MODELS = standins.build(SKELETON, app_label="benchapp", module=__name__)
globals().update({model.__name__: model for model in MODELS.values()})
