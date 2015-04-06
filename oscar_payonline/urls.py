from django.conf.urls import patterns, url
from .views import RedirectView, CallbackView, FailView, SuccessView


urlpatterns = patterns(
    '',
    url(r'^$', RedirectView.as_view(), name='payonline-pay'),
    url(r'^callback/$', CallbackView.as_view(), name='payonline-callback'),
    url(r'^fail/$', FailView.as_view(), name='payonline-fail'),
    url(r'^success/$', SuccessView.as_view(), name='payonline-success'),
)
