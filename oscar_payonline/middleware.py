from django.conf import settings
from django.contrib import messages
from django.utils.functional import SimpleLazyObject, empty
from django.utils.translation import ugettext_lazy as _

from oscar.core.loading import get_class, get_model

from .facade import PayonlineFacade

Order = get_model('order', 'Order')


class FrozenOrderMiddleware(object):

    # Middleware interface methods

    def process_request(self, request):

        # We lazily load the frozen order so use a private variable to hold the
        # cached instance.
        request._order_cache = None

        def load_frozen_order():
            order = self.get_frozen_order(request)
            return order

        def get_payment_url():
            facade = PayonlineFacade()
            return facade.get_redirect_url()

        # Use Django's SimpleLazyObject to only perform the loading work
        # when the attribute is accessed.
        request.frozen_order = SimpleLazyObject(load_frozen_order)
        request.frozen_order_pay_url = SimpleLazyObject(get_payment_url)

    def process_template_response(self, request, response):
        if hasattr(response, 'context_data'):
            if response.context_data is None:
                response.context_data = {}
            if 'frozen_order' not in response.context_data:
                response.context_data['frozen_order'] = request.frozen_order
                response.context_data['frozen_order_pay_url'] = request.frozen_order_pay_url
            else:
                # Occasionally, a view will want to pass an alternative frozen order
                # to be rendered.  This can happen as part of checkout
                # processes where the submitted order is frozen when the
                # customer is redirected to another site (eg PayPal or Payonline).  When the
                # customer returns and we want to show the order thankyou page
                # template, we need to ensure that the frozen order gets
                # rendered (not request.frozen_order).  We still keep a reference to
                # the request frozen order (just in case).
                response.context_data['previous_frozen_order'] = request.frozen_order
        return response

    def get_frozen_order(self, request):
        if request._order_cache is not None:
            return request._order_cache
        facade = PayonlineFacade()
        order = facade.load_frozen_order(request)
        request._order_cache = order
        return order
