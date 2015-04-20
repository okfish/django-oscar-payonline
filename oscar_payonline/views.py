import urllib
import logging

from django.http import (HttpResponseBadRequest,
                         HttpResponseRedirect,
                         HttpResponse)
from django.core.urlresolvers import reverse
from django.db.models import get_model
from django.shortcuts import render, get_object_or_404

from oscar.core.loading import get_class
from oscar.apps.checkout.views import PaymentDetailsView

from payonline import views as payonline_views
from payonline.loader import get_success_backends, get_fail_backends
from sitesutils.helpers import get_site

from .facade import merchant_reference, defrost_basket
from .exceptions import (
    EmptyBasketException, MissingShippingAddressException,
    MissingShippingMethodException, PayOnlineError)

CheckoutSessionMixin = get_class('checkout.session', 'CheckoutSessionMixin')
Basket = get_model('basket', 'Basket')

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
    pass


class SuccessView(PaymentDetailsView):
    template_name_preview = 'oscar_payonline/preview.html'
    preview = True
    
    def get_context_data(self, **kwargs):
        ctx = super(SuccessView, self).get_context_data(**kwargs)

        # This context generation only runs when in preview mode
        ctx.update({
            'frozen_basket': self.basket,
        })
        ctx['shipping_method'] = self.get_shipping_method()
        ctx['order_total_incl_tax'] = D(self.txn.value('PAYMENTREQUEST_0_AMT'))

        return ctx

    def get(self, request, *args, **kwargs):
        """
        Fetch details about the successful transaction from
        PayOnline.  We use these details to show a preview of
        the order with a 'submit' button to place it.
        """


        # Lookup the frozen basket that this txn corresponds to
        try:
            self.basket = Basket.objects.get(id=kwargs['basket_id'],
                                             status=Basket.FROZEN)
        except Basket.DoesNotExist:
            messages.error(
                self.request,
                _("No basket was found that corresponds to your "
                  "PayOnline transaction"))
            return HttpResponseRedirect(reverse('basket:summary'))

        return super(SuccessResponseView, self).get(request, *args, **kwargs)

class FailView(payonline_views.FailView):
    template_name = 'oscar_payonline/fail.html'
    
    def get(self, request, *args, **kwargs):
        basket_id = request.GET['basket_id'] 
        defrost_basket(basket_id)
        if 'ErrorCode' not in request.GET:
            logger.debug("Raw request: %s", request.GET)
            return HttpResponseBadRequest()
        err_code = request.GET['ErrorCode']
        ref_id = request.GET['OrderId']
        txn_id = request.GET['TransactionID']
        logger.info("Failed PayOnline transaction (txn_id=%s, merchant_reference=%s, error_code=%s)",
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
            'error' : ('PayOnline returned an error code: %s' % err_code),
            'error_code': err_code,
        })
    