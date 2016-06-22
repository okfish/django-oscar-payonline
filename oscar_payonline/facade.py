import random

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils.translation import ugettext_lazy as _

from oscar.core.loading import get_class, get_model
from oscar.apps.payment.exceptions import UnableToTakePayment, InvalidGatewayRequestError

from payonline.models import PaymentData
from payonline.helpers import APIErrors

from .exceptions import PayOnlineError

Basket = get_model('basket', 'Basket')
Applicator = get_class('offer.utils', 'Applicator')
Selector = get_class('partner.strategy', 'Selector')


def merchant_reference(merchant_id, basket_id):
    # Ideas stolen from Oscar's Datacash facade

    # Get a random number to append to the end.  This solves the problem
    # where a previous request crashed out and didn't save a model
    # instance.  Hence we can get a clash of merchant references.
    rand = "%04.f" % (random.random() * 10000)
    return u'%s-%s-%s' % (merchant_id, basket_id, rand)


def defrost_basket(basket_id):
    basket = get_object_or_404(Basket, id=basket_id,
                               status=Basket.FROZEN)
    basket.thaw()    


def load_frozen_basket(request, basket_id):
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


def fetch_transaction_details(ref):
    txn = None
    try:
        txn = PaymentData.objects.get(order_id=ref)
    except PaymentData.DoesNotExist:
        msg = "Error for %s: PaymentData does not exists" % ref
        raise PayOnlineError(msg)
    return txn


def confirm_transaction(ref, amount, currency):
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
        txn = fetch_transaction_details(ref)
    except PayOnlineError:
        raise
    if txn.amount != amount or txn.currency != currency:
        msg = ("Error for %s: amount %s or currency %s "
              "does not match requested (%s %s)" % (ref, amount, currency, 
                                                    txn.amount, txn.currency ))
        raise PayOnlineError(msg)
    return txn


def get_error_message(code):
    return APIErrors().get(code)
