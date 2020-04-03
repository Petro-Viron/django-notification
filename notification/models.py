from __future__ import print_function

import logging

import pynliner
from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.sites.models import Site
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, models
from django.db.models.query import QuerySet
from django.template import engines
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import ugettext as _
from django.utils.translation import activate, get_language
from twilio.rest import Client as TwilioRestClient

from .signals import email_sent, sms_sent

try:
    import pickle as pickle
except ImportError:
    import pickle


notifications_logger = logging.getLogger("pivot.notifications")

QUEUE_ALL = getattr(settings, "NOTIFICATION_QUEUE_ALL", False)
TWILIO_ACCOUNT_SID = getattr(settings, "TWILIO_ACCOUNT_SID", False)
TWILIO_ACCOUNT_TOKEN = getattr(settings, "TWILIO_ACCOUNT_TOKEN", False)
TWILIO_CALLER_ID = getattr(settings, "TWILIO_CALLER_ID", False)

if 'guardian' in settings.INSTALLED_APPS:
    enable_object_notifications = True

    def custom_permission_check(perm, obj, user):
        db = user._state.db
        from guardian.models import UserObjectPermission
        return UserObjectPermission.objects.using(db).filter(user=user, permission__codename=perm,
                object_pk = obj.pk, content_type=ContentType.objects.db_manager(db).get_for_model(obj)).exists()

else:
    enable_object_notifications = False

class LanguageStoreNotAvailable(Exception):
    pass

class NoticeType(models.Model):

    label = models.CharField(_('label'), max_length=40)
    display = models.CharField(_('display'), max_length=50)
    description = models.CharField(_('description'), max_length=100)

    # by default only on for media with sensitivity less than or equal to this number
    default = models.IntegerField(_('default'))

    def __str__(self):
        return self.label

    class Meta:
        verbose_name = _("notice type")
        verbose_name_plural = _("notice types")


# if this gets updated, the create() method below needs to be as well...
NOTICE_MEDIA = (
    ("1", _("Email")),
    ("2", _("Display")),
    ("3", _("SMS")),
)

def notice_medium_as_text(medium):
    return dict(NOTICE_MEDIA)[medium]

# how spam-sensitive is the medium
NOTICE_MEDIA_DEFAULTS = {
    "1": 2, # email
    "2": 3,
    "3": 3,
}

class NoticeSetting(models.Model):
    """
    Indicates, for a given user, whether to send notifications
    of a given type to a given medium.
    """

    user = models.ForeignKey(User, verbose_name=_('user'))
    notice_type = models.ForeignKey(NoticeType, verbose_name=_('notice type'))
    medium = models.CharField(_('medium'), max_length=1, choices=NOTICE_MEDIA)
    send = models.BooleanField(_('send'), default=False)

    class Meta:
        verbose_name = _("notice setting")
        verbose_name_plural = _("notice settings")
        unique_together = ("user", "notice_type", "medium")

def get_notification_setting(user, notice_type, medium):
    db = user._state.db
    try:
        return NoticeSetting.objects.using(db).get(user=user, notice_type=notice_type, medium=medium)
    except NoticeSetting.DoesNotExist:
        default = (NOTICE_MEDIA_DEFAULTS[medium] <= notice_type.default)
        try:
            setting = NoticeSetting(user=user, notice_type=notice_type, medium=medium, send=default)
            setting.save(using=db)
        except IntegrityError:
            # We are occassionally getting IntegrityErrors here (possible race condition?)
            # so try getting again
            setting = NoticeSetting.objects.using(db).get(user=user, notice_type=notice_type, medium=medium)
        return setting

def get_all_notification_settings(user):
    db = user._state.db
    return NoticeSetting.objects.using(db).filter(user=user)

def create_notification_setting(user, notice_type, medium):
    db = user._state.db
    default = (NOTICE_MEDIA_DEFAULTS[medium] <= notice_type.default)
    setting = NoticeSetting(user=user, notice_type=notice_type, medium=medium, send=default)
    setting.save(using=db)
    return setting

def should_send(user, notice_type, medium, obj_instance=None):
    if enable_object_notifications and obj_instance:
        has_custom_settings =  custom_permission_check('custom_notification_settings', obj_instance, user)
        if has_custom_settings:
            medium_text = notice_medium_as_text(medium)
            perm_string = "%s-%s"%(medium_text,notice_type.label)
            return custom_permission_check(perm_string, obj_instance, user)
    return get_notification_setting(user, notice_type, medium).send


class NoticeManager(models.Manager):

    def notices_for(self, user, archived=False, unseen=None, on_site=None, sent=False):
        """
        returns Notice objects for the given user.

        If archived=False, it only include notices not archived.
        If archived=True, it returns all notices for that user.

        If unseen=None, it includes all notices.
        If unseen=True, return only unseen notices.
        If unseen=False, return only seen notices.
        """
        if sent:
            lookup_kwargs = {"sender": user}
        else:
            lookup_kwargs = {"recipient": user}
        qs = self.filter(**lookup_kwargs)
        if not archived:
            self.filter(archived=archived)
        if unseen is not None:
            qs = qs.filter(unseen=unseen)
        if on_site is not None:
            qs = qs.filter(on_site=on_site)
        return qs

    def unseen_count_for(self, recipient, **kwargs):
        """
        returns the number of unseen notices for the given user but does not
        mark them seen
        """
        return self.notices_for(recipient, unseen=True, **kwargs).count()

    def received(self, recipient, **kwargs):
        """
        returns notices the given recipient has recieved.
        """
        kwargs["sent"] = False
        return self.notices_for(recipient, **kwargs)

    def sent(self, sender, **kwargs):
        """
        returns notices the given sender has sent
        """
        kwargs["sent"] = True
        return self.notices_for(sender, **kwargs)

class Notice(models.Model):

    recipient = models.ForeignKey(User, related_name='recieved_notices', verbose_name=_('recipient'))
    sender = models.ForeignKey(User, null=True, related_name='sent_notices', verbose_name=_('sender'))
    message = models.TextField(_('message'))
    notice_type = models.ForeignKey(NoticeType, verbose_name=_('notice type'))
    added = models.DateTimeField(_('added'), default=timezone.now, db_index=True)
    unseen = models.BooleanField(_('unseen'), default=True)
    archived = models.BooleanField(_('archived'), default=False)
    on_site = models.BooleanField(_('on site'), default=False)

    objects = NoticeManager()

    def __str__(self):
        return self.message

    def archive(self):
        self.archived = True
        self.save()

    def is_unseen(self):
        """
        returns value of self.unseen but also changes it to false.

        Use this in a template to mark an unseen notice differently the first
        time it is shown.
        """
        unseen = self.unseen
        if unseen:
            self.unseen = False
            self.save()
        return unseen

    class Meta:
        ordering = ["-added"]
        verbose_name = _("notice")
        verbose_name_plural = _("notices")

    def get_absolute_url(self):
        return ("notification_notice", [str(self.pk)])
    get_absolute_url = models.permalink(get_absolute_url)

class NoticeQueueBatch(models.Model):
    """
    A queued notice.
    Denormalized data for a notice.
    """
    pickled_data = models.TextField()

def create_notice_type(label, display, description, default=2, verbosity=1):
    """
    Creates a new NoticeType.

    This is intended to be used by other apps as a post_syncdb manangement step.
    """
    try:
        notice_type = NoticeType.objects.get(label=label)
        updated = False
        if display != notice_type.display:
            notice_type.display = display
            updated = True
        if description != notice_type.description:
            notice_type.description = description
            updated = True
        if default != notice_type.default:
            notice_type.default = default
            updated = True
        if updated:
            notice_type.save()
            if verbosity > 1:
                print("Updated %s NoticeType" % label)
    except NoticeType.DoesNotExist:
        NoticeType(label=label, display=display, description=description, default=default).save()
        if verbosity > 1:
            print("Created %s NoticeType" % label)

def get_notification_language(user):
    """
    Returns site-specific notification language for this user. Raises
    LanguageStoreNotAvailable if this site does not use translated
    notifications.
    """
    if getattr(settings, 'NOTIFICATION_LANGUAGE_MODULE', False):
        try:
            app_label, model_name = settings.NOTIFICATION_LANGUAGE_MODULE.split('.')
            try:
                return getattr(user, model_name.lower()).language
            except AttributeError:
                pass
            model = apps.get_model(app_label=app_label, model_name=model_name)
            language_model = model._default_manager.get(user__id__exact=user.id)
            if hasattr(language_model, 'language'):
                return language_model.language
        except (ImportError, ImproperlyConfigured, model.DoesNotExist):
            raise LanguageStoreNotAvailable
    raise LanguageStoreNotAvailable

def get_formatted_messages(formats, label, context):
    """
    Returns a dictionary with the format identifier as the key. The values are
    are fully rendered templates with the given context.
    """
    format_templates = {}
    for format in formats:

        # conditionally turn off autoescaping for .txt extensions in format
        engine_names = [e.name for e in engines.all()]
        if format.endswith(".txt") and "notification.txt" in engine_names:
            engine = 'notification.txt'
        else:
            engine = None

        format_templates[format] = render_to_string((
            'notification/%s/%s' % (label, format),
            'notification/%s' % format), context=context, using=engine)
    return format_templates

def send_now(users, label, extra_context=None, on_site=True, sender=None, attachments=[],\
        obj_instance=None, force_send=False):
    """
    Creates a new notice.

    This is intended to be how other apps create new notices.

    notification.send(user, 'friends_invite_sent', {
        'spam': 'eggs',
        'foo': 'bar',
    )

    You can pass in on_site=False to prevent the notice emitted from being
    displayed on the site.
    """
    if extra_context is None:
        extra_context = {}

    db = users[0]._state.db

    notice_type = NoticeType.objects.using(db).get(label=label)
    protocol = getattr(settings, "DEFAULT_HTTP_PROTOCOL", "http")
    current_site = Site.objects.db_manager(db).get_current()

    current_language = get_language()

    formats = (
        'short.txt',
        'full.txt',
        'notice.html',
        'full.html',
        'sms.txt',
    ) # TODO make formats configurable

    from django.db import connection

    for user in users:

        should_send_email = user.is_active and (user.email and force_send or should_send(user, notice_type, "1", obj_instance))
        should_send_sms = user.userprofile.sms and user.is_active and should_send(user, notice_type, "3", obj_instance)
        # disabled check for on_site for now since we are not using it
        # on_site = should_send(user, notice_type, "2", obj_instance) #On-site display
        on_site = False

        if not (should_send_email or should_send_sms or on_site):
            continue

        recipients = []
        # get user language for user from language store defined in
        # NOTIFICATION_LANGUAGE_MODULE setting
        try:
            language = get_notification_language(user)
        except LanguageStoreNotAvailable:
            language = None

        if language is not None:
            # activate the user's language
            activate(language)

        # update context with user specific translations
        context = {
            "recipient": user,
            "sender": sender,
            "notice": _(notice_type.display),
            "notices_url": "",
            "current_site": current_site,
        }
        context.update(extra_context)

        # get prerendered format messages
        messages = get_formatted_messages(formats, label, context)
        context['message'] = messages['short.txt']

        # Strip newlines from subject
        subject = ''.join(render_to_string('notification/email_subject.txt', context).splitlines())

        context['message'] = messages['full.txt']
        body = render_to_string('notification/email_body.txt', context)
        body = pynliner.fromString(body)

        notice = Notice.objects.using(db).create(recipient=user, message=messages['notice.html'],
            notice_type=notice_type, on_site=on_site, sender=sender)

        if should_send_email: # Email
            recipients.append(user.email)
            # send empty "plain text" data
            msg = EmailMultiAlternatives(subject, "", settings.DEFAULT_FROM_EMAIL, recipients)
            # attach html data as alternative
            msg.attach_alternative(body, "text/html")
            for attachment in attachments:
                msg.attach(attachment)
            try:
                msg.send()
                email_sent.send(sender=Notice, user=user, notice_type=notice_type, obj=obj_instance)
                notifications_logger.info("SUCCESS:EMAIL:%s: data=(notice_type=%s, subject=%s)"%(user, notice_type, subject))
            except:
                notifications_logger.exception("ERROR:EMAIL:%s: data=(notice_type=%s, subject=%s)"%(user, notice_type, subject))

        if should_send_sms:
            try:
                rc = TwilioRestClient(TWILIO_ACCOUNT_SID, TWILIO_ACCOUNT_TOKEN)
                rc.api.v2010.messages.create(
                    to=user.userprofile.sms,
                    from_=TWILIO_CALLER_ID,
                    body=messages['sms.txt'],
                )
                sms_sent.send(sender=Notice, user=user, notice_type=notice_type, obj=obj_instance)
                notifications_logger.info("SUCCESS:SMS:%s: data=(notice_type=%s, msg=%s)"%(user, notice_type, messages['sms.txt']))
            except:
                notifications_logger.exception("ERROR:SMS:%s: data=(notice_type=%s, msg=%s)"%(user, notice_type, messages['sms.txt']))


    # reset environment to original language
    activate(current_language)

def send(*args, **kwargs):
    """
    A basic interface around both queue and send_now. This honors a global
    flag NOTIFICATION_QUEUE_ALL that helps determine whether all calls should
    be queued or not. A per call ``queue`` or ``now`` keyword argument can be
    used to always override the default global behavior.
    """
    queue_flag = kwargs.pop("queue", False)
    now_flag = kwargs.pop("now", False)
    assert not (queue_flag and now_flag), "'queue' and 'now' cannot both be True."
    if queue_flag:
        return queue(*args, **kwargs)
    elif now_flag:
        return send_now(*args, **kwargs)
    else:
        if QUEUE_ALL:
            return queue(*args, **kwargs)
        else:
            return send_now(*args, **kwargs)

def queue(users, label, extra_context=None, on_site=True, sender=None):
    """
    Queue the notification in NoticeQueueBatch. This allows for large amounts
    of user notifications to be deferred to a seperate process running outside
    the webserver.
    """
    if extra_context is None:
        extra_context = {}
    if isinstance(users, QuerySet):
        users = [row["pk"] for row in users.values("pk")]
    else:
        users = [user.pk for user in users]
    notices = []
    for user in users:
        notices.append((user, label, extra_context, on_site, sender))
    NoticeQueueBatch(pickled_data=pickle.dumps(notices).encode("base64")).save()

class ObservedItemManager(models.Manager):

    def all_for(self, observed, signal):
        """
        Returns all ObservedItems for an observed object,
        to be sent when a signal is emited.
        """
        content_type = ContentType.objects.get_for_model(observed)
        observed_items = self.filter(content_type=content_type, object_id=observed.id, signal=signal)
        return observed_items

    def get_for(self, observed, observer, signal):
        content_type = ContentType.objects.get_for_model(observed)
        observed_item = self.get(content_type=content_type, object_id=observed.id, user=observer, signal=signal)
        return observed_item


class ObservedItem(models.Model):

    user = models.ForeignKey(User, verbose_name=_('user'))

    content_type = models.ForeignKey(ContentType)
    object_id = models.PositiveIntegerField()
    observed_object = GenericForeignKey('content_type', 'object_id')

    notice_type = models.ForeignKey(NoticeType, verbose_name=_('notice type'))

    added = models.DateTimeField(_('added'), default=timezone.now)

    # the signal that will be listened to send the notice
    signal = models.TextField(verbose_name=_('signal'))

    objects = ObservedItemManager()

    class Meta:
        ordering = ['-added']
        verbose_name = _('observed item')
        verbose_name_plural = _('observed items')

    def send_notice(self, extra_context=None):
        if extra_context is None:
            extra_context = {}
        extra_context.update({'observed': self.observed_object})
        send([self.user], self.notice_type.label, extra_context)

def observe(observed, observer, notice_type_label, signal='post_save'):
    """
    Create a new ObservedItem.

    To be used by applications to register a user as an observer for some object.
    """
    notice_type = NoticeType.objects.get(label=notice_type_label)
    observed_item = ObservedItem(user=observer, observed_object=observed,
                                 notice_type=notice_type, signal=signal)
    observed_item.save()
    return observed_item

def stop_observing(observed, observer, signal='post_save'):
    """
    Remove an observed item.
    """
    observed_item = ObservedItem.objects.get_for(observed, observer, signal)
    observed_item.delete()

def send_observation_notices_for(observed, signal='post_save', extra_context=None):
    """
    Send a notice for each registered user about an observed object.
    """
    if extra_context is None:
        extra_context = {}
    observed_items = ObservedItem.objects.all_for(observed, signal)
    for observed_item in observed_items:
        observed_item.send_notice(extra_context)
    return observed_items

def is_observing(observed, observer, signal='post_save'):
    if isinstance(observer, AnonymousUser):
        return False
    try:
        observed_items = ObservedItem.objects.get_for(observed, observer, signal)
        return True
    except ObservedItem.DoesNotExist:
        return False
    except ObservedItem.MultipleObjectsReturned:
        return True

def handle_observations(sender, instance, *args, **kw):
    send_observation_notices_for(instance)
