from decimal import Decimal as D
import urllib
import logging

from django.conf import settings
from django.db.models import Q
from django.http import (HttpResponseBadRequest,
                         HttpResponseRedirect,
                         HttpResponse)
from django.contrib import messages
from django.core.urlresolvers import reverse
from django.shortcuts import render
from django.utils.translation import ugettext as _
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from oscar.core.loading import get_class, get_classes, get_model

from payonline import views as payonline_views
from payonline.loader import get_fail_backends, get_success_backends
from payonline.settings import CONFIG as PAYONLINE_CONFIG
from payonline.forms import PaymentDataForm
from payonline.models import PaymentData

from sitesutils.helpers import get_site

from .facade import PayonlineFacade

from .exceptions import PayOnlineError

UnableToTakePayment = get_class('payment.exceptions', 'UnableToTakePayment')
ThankYouView = get_class('checkout.views', 'ThankYouView')
CheckoutSessionMixin = get_class('checkout.session', 'CheckoutSessionMixin')
InvalidOrderStatus, InvalidPaymentEvent = get_classes('order.exceptions', ('InvalidOrderStatus',
                                                                           'InvalidPaymentEvent'))
EventHandler = get_class('order.processing', 'EventHandler')

Basket = get_model('basket', 'Basket')
Order = get_model('order', 'Order')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
PaymentEventType = get_model('order', 'PaymentEventType')
PaymentEvent = get_model('order', 'PaymentEvent')
PaymentEventQuantity = get_model('order', 'PaymentEventQuantity')


logger = logging.getLogger('payonline')


# the mixin below composed from methods of oscar.apps.checkout.mixins.OrderPlacementMixin
class PaymentHandleMixin(object):

    def add_payment_source(self, source):
        """
        Record a payment source for this order
        """
        if self._payment_sources is None:
            self._payment_sources = []
        self._payment_sources.append(source)

    def add_payment_event(self, event_type_name, amount, reference=''):
        """
        Record a payment event
        """
        event_type, __ = PaymentEventType.objects.get_or_create(
            name=event_type_name)
        # We keep a local cache of (unsaved) payment events
        if self._payment_events is None:
            self._payment_events = []

        event = PaymentEvent(
            event_type=event_type, amount=amount,
            reference=reference)
        if event:
            self._payment_events.append(event)

    def save_payment_details(self, order):
        """
        Saves all payment-related details. This could include a billing
        address, payment sources and any order payment events.
        """
        self.save_payment_events(order)
        self.save_payment_sources(order)

    def save_payment_events(self, order):
        """
        Saves any relevant payment events for this order
        """
        if not self._payment_events:
            return
        for event in self._payment_events:
            event.order = order
            event.save()
        # We assume all lines are involved in the initial payment event
        for line in order.lines.all():
            PaymentEventQuantity.objects.create(
                event=event, line=line, quantity=line.quantity)

    def save_payment_sources(self, order):
        """
        Saves any payment sources used in this order.

        When the payment sources are created, the order model does not exist
        and so they need to have it set before saving.
        """
        if not self._payment_sources:
            return
        for source in self._payment_sources:
            source.order = order
            source.save()

    def set_order_status(self, order, new_status, note_msg=None):
        old_status = order.status
        try:
            EventHandler().handle_order_status_change(order, new_status, note_msg)
        except InvalidOrderStatus:
            logger.error("Can't change order status to: %s. Previous status: %s", new_status, old_status)
        if order.status == new_status:
            logger.warning("Order #%s status changed to %s", order.number, new_status)


class RedirectView(CheckoutSessionMixin, payonline_views.PayView):

    def __init__(self, *args, **kwargs):
        self._order_id = ''
        self.facade = PayonlineFacade()
        super(RedirectView, self).__init__(*args, **kwargs)
        
    def get_order_id(self):
        if not self._order_id:
            # at first try to find appropriate payment event and take its reference
            # assuming it was a frozen order
            # event should be of 'payonline-redirected' type and here we do not check
            #  if previous attempts was finished or not
            payment_events = self.order.payment_events.all().\
                filter(Q(event_type__code=self.facade.EVENT_CODE_REDIRECTED) |
                       Q(event_type__code=self.facade.EVENT_CODE_FAILED) |
                       Q(event_type__code=self.facade.EVENT_CODE_SUCCESSFUL)).order_by('-date_created')
            if payment_events.count() > 0:
                # get previously generated reference number if only last redirection not finished
                # assuming callbacks was not called
                if payment_events[0].event_type.code == self.facade.EVENT_CODE_REDIRECTED:
                    self._order_id = payment_events[0].reference
            else:
                self._order_id = self.facade.merchant_reference(self.order_number)
        return self._order_id

    def get_query_params(self):
        params = super(RedirectView, self).get_query_params()
        params['order_id'] = str(self.order_number)
        return params

    def get_amount(self):
        return u'%.2f' % float(self.order.total_incl_tax)
    
    def get_redirect_url(self, **kwargs):
        params = self.get_query_params()
        return '%s?%s' % (self.get_payonline_url(), urllib.urlencode(params))

    def get_return_url(self):
        site = get_site(self.request)
        return 'http://%s%s?ref=%s' % (site.domain,
                                       reverse('payonline-success', args=(self.order_number,)),
                                       self.payonline_order_id)

    def get_fail_url(self):
        site = get_site(self.request)
        return 'http://%s%s' % (site.domain, reverse('payonline-fail'))

    def get(self, request, *args, **kwargs):
        # allow order_number to be set via GET request
        self.order_number = getattr(request.GET, 'order_number', None) or self.checkout_session.get_order_number()
        if not self.order_number:
            # order_number MUST be set in the session
            # if not we load last frozen order for payment
            frozen_order = self.facade.load_frozen_order(request)
            self.order_number = getattr(frozen_order, 'number')
        try:
            self.order = request.user.orders.get(number__exact=self.order_number)
        except Order.DoesNotExist:
            messages.error(self.request, _("Can't find order with given number: %s") % self.order_number)
            return HttpResponseRedirect(reverse('checkout:payment-method'))

        if self.order:
            old_status = self.order.status
            self.payonline_order_id = self.get_order_id()
            self.payonline_amount = self.get_amount()
            redirect_url = self.get_redirect_url()

            # TODO: DRY it smth like get_redirect_response()
            if old_status == self.facade.FROZEN_STATUS:
                logger.warning("Order already frozen. Another attempt? Redirecting to PayOnline service. "
                               "Flushing checkout session data."
                               "(order_id=%s, redirect_url=%s)",
                               self.payonline_order_id, redirect_url)
                self.checkout_session.flush()
                return HttpResponseRedirect(redirect_url)

            try:
                # TODO: use EventHandler().handle_order_status_change(self, order, new_status, note_msg=None):
                self.order.set_status(self.facade.INITIAL_STATUS)
            except InvalidOrderStatus:
                logger.error("Can't set initial status for order"
                             " (order_number=%s, status=%s)",
                             self.order_number, old_status)
                messages.error(self.request, _("Can't start to process order #%s. "
                                               "Is it possible, that you have paid it already?"
                                               "Please, check order status (%s) and call administrator if "
                                               "if you need a help") % (self.order_number, old_status))
                redirect_url = reverse('customer:order', kwargs={'order_number': self.order_number})
                return HttpResponseRedirect(redirect_url)

            logger.info("Started processing PayOnline request"
                        " (order_id=%s, amount=%s, order_number=%s)",
                        self.payonline_order_id, self.payonline_amount, self.order_number)

            if self.payonline_order_id:
                # TODO: move it to facade.freeze_order or smth like
                try:
                    self.order.set_status(self.facade.FROZEN_STATUS)
                except InvalidOrderStatus:
                    logger.error("Can't set FROZEN status for order"
                                " (order_number=%s, status=%s)",
                                self.order_number, old_status)
                    messages.error(self.request, _("Can't freeze order #%s. "
                                                   "Please, check order status (%s) and call administrator if "
                                                   "if you need a help") % self.order_number, old_status)

                if self.order.status == self.facade.FROZEN_STATUS:
                    logger.info("Order frozen. Redirecting to PayOnline service"
                                "(order_id=%s, redirect_url=%s)",
                                self.payonline_order_id, redirect_url)
                    self.checkout_session.flush()
                else:
                    redirect_url = reverse('customer:order', kwargs={'order_number': self.order_number})
                return HttpResponseRedirect(redirect_url)
        logger.debug("Raw request: %s", request.GET)
        return HttpResponseBadRequest()


class CallbackView(PaymentHandleMixin, payonline_views.CallbackView):

    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        self._payment_events = None
        self._payment_sources = None
        logger.info("Received a call from PayOnline service. Dispatching...")
        return super(CallbackView, self).dispatch(*args, **kwargs)

    def process_form(self, form):
        """
        Complete payment with PayOnline - this should compare local txn data
        and PayOnline txn info using API method to capture
        the money from the initial transaction.
        TODO: remote call to PayOnline
        """
        order = None
        facade = PayonlineFacade()

        if form.is_valid():
            txn_id = form.cleaned_data.get('transaction_id')
            if not PaymentData.objects.filter(transaction_id=txn_id).exists():
                payment_data = form.save()

                if not getattr(payment_data, 'pk') > 0:
                    logger.error("Can't save payment data received. Txn ID: %s", txn_id)
                    return HttpResponseBadRequest()

                ref = payment_data.order_id  # meaning payonline's order id which is merchant reference
                amount = payment_data.amount
                currency = payment_data.currency

                # TODO: confirm transaction via Payonline API request

                # Record payment source and event
                source_type, is_created = SourceType.objects.get_or_create(
                    name='payonline')
                source = Source(source_type=source_type,
                                currency=currency,
                                amount_allocated=amount,
                                amount_debited=amount,
                                reference=ref)
                self.add_payment_source(source)
                self.add_payment_event(facade.EVENT_CODE_SUCCESSFUL, amount,
                                       reference=ref)

                # move order to Payment successful status
                try:
                    order = facade.validate_order(ref)
                except PayOnlineError as e:
                    logger.error(
                        "Payment event not saved. Can't find order for reference %s: Reason: %s", ref, e)
                if order:
                    logger.info(
                        "Payment event saved for order #%s (type:%s, amount:%s, ref: %s)",
                        order.number, source_type, amount, ref)
                    self.save_payment_details(order)
                    note_msg = _("Successful payment information received from Payonline."
                                 "Transaction ID: %s. Order status changed" % txn_id)
                    self.set_order_status(order, facade.SUCCESSFUL_STATUS, note_msg)

                # for backward compatibility
                backends = get_success_backends()
                for backend in backends:
                    backend(payment_data)

                return HttpResponse()
            else:
                logger.error(
                        "Strange situation! Transaction already saved. Check it! (txn_id:%s)", txn_id)
                return HttpResponseBadRequest()
        else:
            logger.error("Received invalid callback request! Raw data: %s", form.data)
            return HttpResponseBadRequest()


class SuccessView(ThankYouView):

    def get_context_data(self, **kwargs):
        ctx = super(SuccessView, self).get_context_data(**kwargs)

        # This context generation only runs when in preview mode
        if hasattr(self, 'txn'):
            ctx.update({
                'merchant_reference': self.merchant_ref,
                'payonline_order_id': self.txn.order_id,
                'payonline_amount': D(self.txn.amount),
                'payonline_provider': self.txn.provider_name,
                'payonline_provider_code': self.txn.provider,
            })
        return ctx

    def get(self, request, *args, **kwargs):
        """
        Fetch details about the successful transaction from
        PayOnline.  We use these details to show a preview of
        the order with a 'submit' button to place it.
        """

        facade = PayonlineFacade()
        try:
            self.merchant_ref = request.GET['ref']
        except KeyError:
            # Manipulation - redirect to basket page with warning message
            logger.warning("Missing GET params on success response page")
            messages.error(
                self.request,
                _("Unable to determine PayOnline transaction details"))
            return HttpResponseRedirect(reverse('customer:order-list'))
        
        try:
            self.txn = facade.fetch_transaction_details(self.merchant_ref)
        except PayOnlineError as e:
            logger.warning(
                "Unable to fetch transaction details for reference %s: %s",
                self.merchant_ref, e)
            messages.error(
                self.request,
                _("Sorry. We have not received payment confirmation yet. But hopes, it will happen soon."))

        return super(SuccessView, self).get(request, *args, **kwargs)


class FailView(PaymentHandleMixin, payonline_views.FailView):
    template_name = "oscar_payonline/fail.html"

    def get_private_security_key(self):
        return PAYONLINE_CONFIG['PRIVATE_SECURITY_KEY']

    def get_form(self, data):
        return PaymentDataForm(
            data=data, private_security_key=self.get_private_security_key())

    def get(self, request, *args, **kwargs):
        order = None
        if 'ErrorCode' not in request.GET:
            logger.error("Strange failback request. Raw request: %s", request.GET)
            return HttpResponseBadRequest()

        form = self.get_form(request.GET)

        if form.is_valid():
            facade = PayonlineFacade()
            txn_id = form.cleaned_data.get('transaction_id')
            err_code = str(request.GET['ErrorCode'])
            ref_id = form.cleaned_data.get('order_id')
            order_id = request.GET['order_id']
            err_msg = facade.get_error_message(err_code)
            logger.info("Failed PayOnline transaction (txn_id=%s,"
                     "merchant_reference=%s, error_code=%s)",
                     txn_id, ref_id, err_code)

            if not PaymentData.objects.filter(transaction_id=txn_id).exists():
                logger.warning("It's a strange situation when no PaymentData was saved "
                               "before the transaction has begun. (txn_id=%s,"
                               "merchant_reference=%s, error_code=%s)",
                               txn_id, ref_id, err_code)
                # form.save()

            try:
                order = Order.objects.get(number=order_id)
            except Order.DoesNotExist:
                logger.error("Can't find Order #%s for failed transaction %s", order_id, txn_id)
                return HttpResponseBadRequest()
            if order is not None:
                note_msg = _("Payment for order #%(number)s failed. Reason:  %(msg)s" % {'number': order_id,
                                                                                         'msg': err_msg})
                new_status = facade.FAILED_STATUS
                self.set_order_status(order, new_status, note_msg)
            return HttpResponse()
        else:
            logger.error("Received invalid request from Payonline gateway. "
                         "Checksum not valid or something goes wrong. Raw request: %s" % request.GET)
            return HttpResponseBadRequest()

    def post(self, request, *args, **kwargs):
        if 'ErrorCode' not in request.POST:
            return HttpResponseBadRequest()
        err_code = request.POST['ErrorCode']
        backends = get_fail_backends()
        for backend in backends:
            backend(request, err_code)
        
        return render(request, self.template_name, {
            'error': PayonlineFacade().get_error_message(err_code),
            'error_code': err_code,
        })
