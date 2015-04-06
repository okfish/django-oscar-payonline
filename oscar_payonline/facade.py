from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from oscar.apps.payment.exceptions import UnableToTakePayment, InvalidGatewayRequestError

class Facade(object):
    """
    A bridge between oscar's objects and the payonline gateway object
    """
    pass