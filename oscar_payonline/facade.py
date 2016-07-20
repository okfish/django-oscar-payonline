import random

from django.conf import settings
from django.core.urlresolvers import reverse
from django.shortcuts import get_object_or_404
from django.utils.translation import ugettext_lazy as _

from oscar.core.loading import get_class, get_model
from oscar.apps.payment.exceptions import UnableToTakePayment, InvalidGatewayRequestError

from payonline.settings import CONFIG as PAYONLINE_CONFIG
from payonline.models import PaymentData
from payonline.helpers import APIErrors

from .exceptions import PayOnlineError

Basket = get_model('basket', 'Basket')
Order = get_model('order', 'Order')
PaymentEvent = get_model('order', 'PaymentEvent')
Applicator = get_class('offer.utils', 'Applicator')
Selector = get_class('partner.strategy', 'Selector')

# as we now have payment processing after the order placement
# some additional options required to check if we can start the process or not
# we will try to change order status to initial
# and redirect to order view if InvalidOrderStatus exception caught
FROZEN_PAYONLINE_STATUS = getattr(settings, 'OSCAR_FROZEN_PAYONLINE_STATUS', 'Frozen for payment')
FAILED_PAYONLINE_STATUS = getattr(settings, 'OSCAR_FAILED_PAYONLINE_STATUS', 'Failed payment')
SUCCESSFUL_PAYONLINE_STATUS = getattr(settings, 'OSCAR_SUCCESSFUL_PAYONLINE_STATUS', 'Successful payment')
INITIAL_PAYONLINE_STATUS = getattr(settings, 'OSCAR_INITIAL_PAYONLINE_STATUS', settings.OSCAR_INITIAL_ORDER_STATUS)


class PayonlineFacade(object):

    def __init__(self):
        self.FROZEN_STATUS = FROZEN_PAYONLINE_STATUS
        self.FAILED_STATUS = FAILED_PAYONLINE_STATUS
        self.SUCCESSFUL_STATUS = SUCCESSFUL_PAYONLINE_STATUS
        self.INITIAL_STATUS = INITIAL_PAYONLINE_STATUS
        self.EVENT_CODE_REDIRECTED = 'payonline-redirected'
        self.EVENT_CODE_FAILED = 'payonline-failed'
        self.EVENT_CODE_SUCCESSFUL = 'payonline-successful'

    def get_redirect_url(self):
        return reverse('payonline-pay')

    def get_merchant_id(self):
        return PAYONLINE_CONFIG['MERCHANT_ID']

    def merchant_reference(self, basket_id):
        # Ideas stolen from Oscar's Datacash facade

        # Get a random number to append to the end.  This solves the problem
        # where a previous request crashed out and didn't save a model
        # instance.  Hence we can get a clash of merchant references.
        merchant_id = self.get_merchant_id()
        rand = "%04.f" % (random.random() * 10000)
        return u'%s-%s-%s' % (merchant_id, basket_id, rand)

    def defrost_basket(self, basket_id):
        basket = get_object_or_404(Basket, id=basket_id,
                                   status=Basket.FROZEN)
        basket.thaw()

    def load_frozen_basket(self, request, basket_id):
        # Ideas stolen from Oscar's PayPal facade

        # Lookup the frozen basket that this txn corresponds to
        try:
            basket = Basket.objects.get(id=basket_id, status=Basket.FROZEN)
        except Basket.DoesNotExist:
            return None

        # Assign strategy to basket instance
        if Selector:
            basket.strategy = Selector().strategy(request, request.user)

        # Re-apply any offers
        Applicator().apply(basket, request.user, request)

        return basket

    def validate_order(self, ref):
        events = PaymentEvent.objects.all().\
            filter(reference=ref).\
            filter(event_type__name=self.EVENT_CODE_REDIRECTED)
        if not events:
            msg = ("Error for reference #%s: 'payonline-redirected' event not found" % ref)
            raise PayOnlineError(msg)
        if events.count() > 1:
            msg = ("Error for reference #%s: too many 'payonline-redirected' events found" % ref)
            raise PayOnlineError(msg)
        if not getattr(events[0], 'order'):
            msg = ("Error for reference #%s: 'payonline-redirected' event has no order set" % ref)
            raise PayOnlineError(msg)
        return events[0].order

    def load_frozen_order(self, request):
        # Lookup the frozen order for user
        #
        orders = request.user.orders.all().filter(status=self.FROZEN_STATUS).order_by('-date_placed')
        if orders.count() > 0:
            # always return last placed frozen order as it must be the only one
            return orders[0]
        else:
            return None

    def fetch_transaction_details(self, ref):
        txn = None
        try:
            txn = PaymentData.objects.get(order_id=ref)
        except PaymentData.DoesNotExist:
            msg = "Error for %s: PaymentData does not exists" % ref
            raise PayOnlineError(msg)
        return txn

    def confirm_transaction(self, ref, amount, currency):
        """
        Confirms that transaction corrensponding to given ref-string
        exists in our database (the callback has been triggered)
        and TODO: Payonline should return the same info.
        Otherwise, two conditions can be reached:
         - we have txn recorded but no txn found via PayOnline API
           so we mark txn as suspicious
         - we have no txn records but Payonline tells that txn is ok
           (e.g. txn was ok but no callbacks triggered or data saved)
           so we should save it again or mark that txn as requiring OP confirmation
        """
        try:
            txn = self.fetch_transaction_details(ref)
        except PayOnlineError:
            raise
        if txn.amount != amount or txn.currency != currency:
            msg = ("Error for %s: amount %s or currency %s "
                  "does not match requested (%s %s)" % (ref, amount, currency,
                                                        txn.amount, txn.currency ))
            raise PayOnlineError(msg)
        return txn

    def get_error_message(self, code):
        return APIErrors().get(code)
