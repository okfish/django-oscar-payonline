from decimal import Decimal as D
import urllib
import logging

from django.http import (HttpResponseBadRequest,
                         HttpResponseRedirect,
                         HttpResponse)
from django.contrib import messages
from django.core.urlresolvers import reverse
from django.shortcuts import render
from django.utils.translation import ugettext as _
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from oscar.core.loading import get_class, get_model

from payonline import views as payonline_views
from payonline.loader import get_fail_backends

from sitesutils.helpers import get_site

from .facade import (merchant_reference, 
                     defrost_basket, 
                     load_frozen_basket,
                     fetch_transaction_details,
                     confirm_transaction,
                     get_error_message,)

from .exceptions import (
    EmptyBasketException, PayOnlineError)

UnableToTakePayment = get_class('payment.exceptions', 'UnableToTakePayment')
PaymentDetailsView = get_class('checkout.views', 'PaymentDetailsView')
CheckoutSessionMixin = get_class('checkout.session', 'CheckoutSessionMixin')
Basket = get_model('basket', 'Basket')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')

logger = logging.getLogger('payonline')


class RedirectView(CheckoutSessionMixin, payonline_views.PayView):

    def __init__(self, *args, **kwargs):
        self._order_id = ''
        super(RedirectView, self).__init__(*args, **kwargs)
        
    def get_order_id(self):
        basket_id = self.request.basket.id
        merchant_id = self.get_merchant_id()
        if not self._order_id:
            self._order_id = merchant_reference(merchant_id, basket_id)
        return self._order_id

    def get_query_params(self):
        params = super(RedirectView, self).get_query_params()
        params['basket_id'] = str(self.request.basket.id)
        return params

    def get_amount(self):
        shipping_charge = self.get_shipping_charge(self.request.basket)
        total = self.get_order_totals(self.request.basket, shipping_charge)
        return u'%.2f' % float(total.incl_tax)
    
    def get_redirect_url(self, **kwargs):
        try:
            url = self._get_redirect_url(**kwargs)
        except PayOnlineError:
            messages.error(self.request, _("An error occurred communicating with PayOnline.ru"))
            url = reverse('checkout:payment-details')
            return url
        except EmptyBasketException:
            messages.error(self.request, _("Your basket is empty"))
            return reverse('basket:summary')
#         except MissingShippingAddressException:
#             messages.error(self.request, _("A shipping address must be specified"))
#             return reverse('checkout:shipping-address')
#         except MissingShippingMethodException:
#             messages.error(self.request, _("A shipping method must be specified"))
#             return reverse('checkout:shipping-method')
        else:
            # Transaction successfully registered with PayOnline.  Now freeze the
            # basket so it can't be edited while the customer is on the PayOnline
            # site.
            self.request.basket.freeze()
            return url
    
    def _get_redirect_url(self, **kwargs):
        basket = self.request.basket
        if basket.is_empty:
            raise EmptyBasketException()
        
        params = self.get_query_params()
        return '%s?%s' % (self.get_payonline_url(), urllib.urlencode(params))

    def get_return_url(self):
        site = get_site(self.request)
        basket_id = self.request.basket.id
        return 'http://%s%s?ref=%s' % (site.domain, 
                                       reverse('payonline-success', args=(basket_id,)),
                                       self.payonline_order_id)

    def get_fail_url(self):
        site = get_site(self.request)
        return 'http://%s%s' % (site.domain, reverse('payonline-fail'))

    def get(self, request, *args, **kwargs):
        payonline_order_id = self.get_order_id()
        payonline_amount = self.get_amount()
        basket_id = self.request.basket.id
        logger.info("Started processing PayOnline request"
                    " (order_id=%s, amount=%s, basket_id=%s)",
                    payonline_order_id, payonline_amount, basket_id)
        
        if payonline_order_id:
            self.payonline_order_id = payonline_order_id
            self.payonline_amount = payonline_amount
            redirect_url = self.get_redirect_url()
            logger.info("Redirecting to PayOnline service"
                        "(order_id=%s, redirect_url=%s)",
                        payonline_order_id, redirect_url)
            return HttpResponseRedirect(redirect_url)
        logger.debug("Raw request: %s", request.GET)
        return HttpResponseBadRequest()

class CallbackView(payonline_views.CallbackView):
    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        logger.info("Received a call from PayOnline service. Dispatching...")
        return super(CallbackView, self).dispatch(*args, **kwargs)


class SuccessView(PaymentDetailsView):
    template_name_preview = 'oscar_payonline/preview.html'
    preview = True
    basket = None
    
    # We don't have the usual pre-conditions (Oscar 1.0+ supported only)
    @property
    def pre_conditions(self):
        return []
            
    def get_context_data(self, **kwargs):
        ctx = super(SuccessView, self).get_context_data(**kwargs)

        # This context generation only runs when in preview mode
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
        error_msg = _(
            "We cannot find transaction details or "
            "a problem occurred communicating with PayOnline "
            "- please try again later"
        )
        
        try:
            self.merchant_ref = request.GET['ref']
        except KeyError:
            # Manipulation - redirect to basket page with warning message
            logger.warning("Missing GET params on success response page")
            messages.error(
                self.request,
                _("Unable to determine PayOnline transaction details"))
            return HttpResponseRedirect(reverse('basket:summary'))
        
        try:
            self.txn = fetch_transaction_details(self.merchant_ref)
        except PayOnlineError as e:
            logger.warning(
                "Unable to fetch transaction details for reference %s: %s",
                self.merchant_ref, e)
            messages.error(
                self.request,
                _("Couldnt find confrimed transaction"
                  " - please try again later or choose other method"))
            return HttpResponseRedirect(reverse('checkout:payment-method'))
        
        # Reload frozen basket which is specified in the URL
        kwargs['basket'] = load_frozen_basket(request, kwargs['basket_id'])
        
        if not kwargs['basket']:
            logger.warning(
                "Unable to load frozen basket with ID %s", kwargs['basket_id'])
            messages.error(
                self.request,
                _("No basket was found that corresponds to your "
                  "PayOnline transaction"))
            return HttpResponseRedirect(reverse('basket:summary'))

        logger.info(
            "Basket #%s - showing preview with payment reference %s",
            kwargs['basket'].id, self.merchant_ref)
        self.preview = True
        return super(SuccessView, self).get(request, *args, **kwargs)
    
    # Two methods below based on Oscar's PayPal extension
    def post(self, request, *args, **kwargs):
        """
        Place an order.

        We fetch the txn details again and then proceed with oscar's standard
        payment details view for placing the order.
        """
        error_msg = _(
            "A problem occurred communicating with PayOnline "
            "- please try again later"
        )
        try:
            self.merchant_ref = request.POST['ref']
        except KeyError:
            # Probably suspicious manipulation if we get here
            messages.error(self.request, error_msg)
            logger.error(
                "Suspicious! No POST params in the request %s", self.request)
            return HttpResponseRedirect(reverse('checkout:payment-method'))

        try:
            self.txn = fetch_transaction_details(self.merchant_ref)
        except PayOnlineError as e:
            # Unable to fetch txn details from PayPal - we have to bail out
            messages.error(self.request, error_msg)
            logger.warning(
                "Unable to fetch transaction with ID %s: %s", self.merchant_ref, e)
            return HttpResponseRedirect(reverse('checkout:payment-method'))

        # Reload frozen basket which is specified in the URL
        basket = load_frozen_basket(request, kwargs['basket_id'])
        if not basket:
            logger.error(
                "Unable to load frozen basket with ID %s", kwargs['basket_id'])
            messages.error(self.request, error_msg)
            return HttpResponseRedirect(reverse('basket:summary'))
        logger.info(
                "Start order submission for basket with ID %s", kwargs['basket_id'])
        submission = self.build_submission(basket=basket)
        return self.submit(**submission)

    def build_submission(self, **kwargs):
        submission = super(
            SuccessView, self).build_submission(**kwargs)
        # Pass the user email so it can be stored with the order
        submission['order_kwargs']['guest_email'] = submission['user'].email
        # Pass PayOnline params
        submission['payment_kwargs']['ref'] = self.merchant_ref
        submission['payment_kwargs']['txn'] = self.txn
        return submission

    def handle_payment(self, order_number, total, **kwargs):
        """
        Complete payment with PayOnline - this should compare local txn data
        and PayOnline txn info using API method to capture 
        the money from the initial transaction.
        TODO: remote call to PayOnline
        """
        try:
            confirm_txn = confirm_transaction(
                kwargs['ref'], kwargs['txn'].amount,
                kwargs['txn'].currency)
        except PayOnlineError as e:
            logger.error(
                "Transaction #%s not confirmed. Errors: %s", kwargs['ref'], e)
            raise UnableToTakePayment()
        if not confirm_txn.transaction_id:
            logger.error(
                "Cant find transaction ID for #%s ", kwargs['ref'])
            raise UnableToTakePayment()

        # Record payment source and event
        source_type, is_created = SourceType.objects.get_or_create(
            name='PayOnline')
        source = Source(source_type=source_type,
                        currency=confirm_txn.currency,
                        amount_allocated=confirm_txn.amount,
                        amount_debited=confirm_txn.amount,
                        reference=confirm_txn.order_id)
        self.add_payment_source(source)
        self.add_payment_event('Settled', confirm_txn.amount,
                               reference=confirm_txn.order_id)
        logger.info(
                "Payment event saved (type:%s, amount:%s, ref: %s)",
                source_type,
                confirm_txn.amount,
                confirm_txn.order_id)

    def get_error_response(self):
        # We bypass the normal session checks for shipping address and shipping
        # method as they don't apply here.
        pass


class FailView(payonline_views.FailView):
    template_name = 'oscar_payonline/fail.html'
    
    def get(self, request, *args, **kwargs):
        if 'basket_id' not in request.GET:
            logger.error("Failed callback from PayOnline service: no basket_id given")
            logger.debug("Raw request: %s", request.GET)
            return HttpResponseBadRequest()
        basket_id = request.GET['basket_id'] 
        defrost_basket(basket_id)
        if 'ErrorCode' not in request.GET:
            logger.debug("Raw request: %s", request.GET)
            return HttpResponseBadRequest()
        err_code = request.GET['ErrorCode']
        ref_id = request.GET['OrderId']
        txn_id = request.GET['TransactionID']
        logger.error("Failed PayOnline transaction (txn_id=%s," 
                     "merchant_reference=%s, error_code=%s)",
                     txn_id, ref_id, err_code)
        return HttpResponse()
        
    def post(self, request, *args, **kwargs):
        if 'ErrorCode' not in request.POST:
            return HttpResponseBadRequest()
        err_code = request.POST['ErrorCode']
        backends = get_fail_backends()
        for backend in backends:
            backend(request, err_code)
        
        return render(request, self.template_name, {
            'error': get_error_message(err_code),
            'error_code': err_code,
        })
