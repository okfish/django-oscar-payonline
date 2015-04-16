from django.apps import AppConfig
from django.utils.translation import ugettext_lazy as _


class OscarPayonlineConfig(AppConfig):
    label = 'oscar_payonline'
    name = 'oscar_payonline'
    verbose_name = _('Payonline extension for Oscar')

