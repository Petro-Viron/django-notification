import django.dispatch

email_sent = django.dispatch.Signal(providing_args=[
    'user', 'notice_type', 'obj',
])

sms_sent = django.dispatch.Signal(providing_args=[
    'user', 'notice_type', 'obj',
])
