from oscar.apps.payment.exceptions import PaymentError


class PayOnlineError(PaymentError):
    pass


class EmptyBasketException(Exception):
    pass


class MissingShippingAddressException(Exception):
    pass


class MissingShippingMethodException(Exception):
    pass
