from __future__ import unicode_literals

import json
import mimetypes
import re
import requests
import six
import time
import nexmo

from django.conf import settings
from django.core.files import File
from django.core.files.temp import NamedTemporaryFile
from django.core.urlresolvers import reverse
from django.utils.http import urlencode
from django.utils.translation import ugettext_lazy as _
from temba.contacts.models import Contact, URN
from temba.flows.models import Flow
from temba.ivr.models import IVRCall
from temba.temba_nexmo import NexmoClient as NexmoCli
from twilio import TwilioRestException
from twilio.rest import TwilioRestClient
from twilio.util import RequestValidator


class IVRException(Exception):
    pass


class NexmoClient(NexmoCli):

    def __init__(self, api_key, api_secret, app_id, app_private_key, org=None):
        self.org = org
        NexmoCli.__init__(self, api_key, api_secret, app_id, app_private_key)

    def validate(self, request):
        return True

    def start_call(self, call, to, from_, status_callback):
        url = 'https://%s%s' % (settings.TEMBA_HOST, reverse('ivr.ivrcall_handle', args=[call.pk]))

        params = dict()
        params['answer_url'] = [url]
        params['answer_method'] = 'POST'
        params['to'] = [dict(type='phone', number=to.strip('+'))]
        params['from'] = dict(type='phone', number=from_.strip('+'))
        params['event_url'] = ["%s?has_event=1" % url]
        params['event_method'] = "POST"

        try:
            if call.parent is None:
                response = self.create_call(params=params)
                conversation_uuid = response.get('conversation_uuid')
                call.external_id = unicode(conversation_uuid)
                call.save()
        except nexmo.Error as e:
            raise IVRException(_("Nexmo call failed, with error %s") % e.message)

    def download_media(self, media_url):
        """
        Fetches the recording and stores it with the provided recording_id
        :param media_url: the url where the media lives
        :return: the url for our downloaded media with full content type prefix
        """
        attempts = 0
        while attempts < 4:
            response = self.download_recording(media_url)

            # in some cases Twilio isn't ready for us to fetch the recording URL yet, if we get a 404
            # sleep for a bit then try again up to 4 times
            if response.status_code == 200:
                break
            else:
                attempts += 1
                time.sleep(.250)

        disposition = response.headers.get('Content-Disposition', None)
        content_type = response.headers.get('Content-Type', None)

        if content_type:
            extension = None
            if disposition == 'inline':
                extension = mimetypes.guess_extension(content_type)
                extension = extension.strip('.')
            elif disposition:
                filename = re.findall("filename=\"(.+)\"", disposition)[0]
                extension = filename.rpartition('.')[2]
            elif content_type == 'audio/x-wav':
                extension = 'wav'

            temp = NamedTemporaryFile(delete=True)
            temp.write(response.content)
            temp.flush()

            return '%s:%s' % (content_type, self.org.save_media(File(temp), extension))

        return None


class TwilioClient(TwilioRestClient):

    def __init__(self, account, token, org=None, **kwargs):
        self.org = org
        super(TwilioClient, self).__init__(account=account, token=token, **kwargs)

    def start_call(self, call, to, from_, status_callback):
        if not settings.SEND_CALLS:
            raise IVRException("SEND_CALLS set to False, skipping call start")

        try:
            twilio_call = self.calls.create(to=to,
                                            from_=call.channel.address,
                                            url=status_callback,
                                            status_callback=status_callback)
            call.external_id = six.text_type(twilio_call.sid)
            call.save()
        except TwilioRestException as twilio_error:
            message = 'Twilio Error: %s' % twilio_error.msg
            if twilio_error.code == 20003:
                message = _('Could not authenticate with your Twilio account. Check your token and try again.')

            raise IVRException(message)

    def validate(self, request):  # pragma: needs cover
        validator = RequestValidator(self.auth[1])
        signature = request.META.get('HTTP_X_TWILIO_SIGNATURE', '')

        base_url = settings.TEMBA_HOST
        url = "https://%s%s" % (base_url, request.get_full_path())
        return validator.validate(url, request.POST, signature)

    def download_media(self, media_url):
        """
        Fetches the recording and stores it with the provided recording_id
        :param media_url: the url where the media lives
        :return: the url for our downloaded media with full content type prefix
        """
        attempts = 0
        while attempts < 4:
            response = requests.get(media_url, stream=True, auth=self.auth)

            # in some cases Twilio isn't ready for us to fetch the recording URL yet, if we get a 404
            # sleep for a bit then try again up to 4 times
            if response.status_code == 200:
                break
            else:
                attempts += 1
                time.sleep(.250)

        disposition = response.headers.get('Content-Disposition', None)
        content_type = response.headers.get('Content-Type', None)

        if content_type:
            extension = None
            if disposition == 'inline':
                extension = mimetypes.guess_extension(content_type)
                extension = extension.strip('.')
            elif disposition:
                filename = re.findall("filename=\"(.+)\"", disposition)[0]
                extension = filename.rpartition('.')[2]
            elif content_type == 'audio/x-wav':
                extension = 'wav'

            temp = NamedTemporaryFile(delete=True)
            temp.write(response.content)
            temp.flush()

            return '%s:%s' % (content_type, self.org.save_media(File(temp), extension))

        return None  # pragma: needs cover


class VerboiceClient:  # pragma: needs cover

    def __init__(self, channel):
        self.endpoint = 'https://verboice.instedd.org/api/call'

        config = json.loads(channel.config)
        self.auth = (config.get('username', None), config.get('password', None))

        # this is the verboice channel, not our channel
        self.verboice_channel = config.get('channel', None)

    def validate(self, request):
        # verboice isn't smart here
        return True

    def start_call(self, call, to, from_, status_callback):
        if not settings.SEND_CALLS:
            raise IVRException("SEND_CALLS set to False, skipping call start")

        channel = call.channel
        Contact.get_or_create(channel.org, channel.created_by, urns=[URN.from_tel(to)])

        # Verboice differs from Twilio in that they expect the first block of twiml up front
        payload = unicode(Flow.handle_call(call))

        # now we can post that to verboice
        url = "%s?%s" % (self.endpoint, urlencode(dict(channel=self.verboice_channel, address=to)))
        response = requests.post(url, data=payload, auth=self.auth).json()

        if 'call_id' not in response:
            raise IVRException(_('Verboice connection failed.'))

        # store the verboice call id in our IVRCall
        call.external_id = response['call_id']
        call.status = IVRCall.IN_PROGRESS
        call.save()
