import random

from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from oscar.apps.payment.exceptions import UnableToTakePayment, InvalidGatewayRequestError

from payonline.models import PaymentData

def merchant_reference(merchant_id, basket_id):
    # Ideas stolen from Oscar's Datacash facade

    # Get a random number to append to the end.  This solves the problem
    # where a previous request crashed out and didn't save a model
    # instance.  Hence we can get a clash of merchant references.
    rand = "%04.f" % (random.random() * 10000)
    return u'%s-%s-%s' % (merchant_id, basket_id, rand)

class Facade(object):
    """
    A bridge between oscar's objects and the payonline gateway object
    """
    pass