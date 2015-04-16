import urllib
import logging

from django.http import (HttpResponseBadRequest,
                         HttpResponseRedirect,
                         HttpResponse)
from django.core.urlresolvers import reverse

from oscar.core.loading import get_class
from oscar.apps.checkout.views import PaymentDetailsView

CheckoutSessionMixin = get_class('checkout.session', 'CheckoutSessionMixin')

from payonline import views as payonline_views
from sitesutils.helpers import get_site


from .facade import merchant_reference
from .exceptions import (
    EmptyBasketException, MissingShippingAddressException,
    MissingShippingMethodException, PayOnlineError)

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
        #return unicode(self.request.session.get('payonline_order_id', ''))

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
        if payonline_order_id:
            self.payonline_order_id = payonline_order_id
            self.payonline_amount = payonline_amount
            redirect_url = self.get_redirect_url()
            return HttpResponseRedirect(redirect_url)
        return HttpResponseBadRequest()

class CallbackView(payonline_views.CallbackView):
    pass


class SuccessView(PaymentDetailsView):
    template_name_preview = 'oscar_payonline/preview.html'
    preview = True
    

class FailView(payonline_views.FailView):
    pass
    