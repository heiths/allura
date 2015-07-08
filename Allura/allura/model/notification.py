#       Licensed to the Apache Software Foundation (ASF) under one
#       or more contributor license agreements.  See the NOTICE file
#       distributed with this work for additional information
#       regarding copyright ownership.  The ASF licenses this file
#       to you under the Apache License, Version 2.0 (the
#       "License"); you may not use this file except in compliance
#       with the License.  You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#       Unless required by applicable law or agreed to in writing,
#       software distributed under the License is distributed on an
#       "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#       KIND, either express or implied.  See the License for the
#       specific language governing permissions and limitations
#       under the License.

'''Manage notifications and subscriptions

When an artifact is modified:

- Notification generated by tool app
- Search is made for subscriptions matching the notification
- Notification is added to each matching subscriptions' queue

Periodically:

- For each subscriptions with notifications and direct delivery:
   - For each notification, enqueue as a separate email message
   - Clear subscription's notification list
- For each subscription with notifications and delivery due:
   - Enqueue one email message with all notifications
   - Clear subscription's notification list

'''

import logging
from bson import ObjectId
from datetime import datetime, timedelta
from collections import defaultdict

from pylons import tmpl_context as c, app_globals as g
from tg import config
import pymongo
import jinja2
from paste.deploy.converters import asbool

from ming import schema as S
from ming.orm import FieldProperty, ForeignIdProperty, RelationProperty, session
from ming.orm.declarative import MappedClass

from allura.lib import helpers as h
from allura.lib import security
from allura.lib.utils import take_while_true
import allura.tasks.mail_tasks

from .session import main_orm_session
from .auth import User, AlluraUserProperty


log = logging.getLogger(__name__)

MAILBOX_QUIESCENT = None  # Re-enable with [#1384]: timedelta(minutes=10)


class Notification(MappedClass):

    '''
    Temporarily store notifications that will be emailed or displayed as a web flash.
    This does not contain any recipient information.
    '''

    class __mongometa__:
        session = main_orm_session
        name = 'notification'
        indexes = ['project_id']

    _id = FieldProperty(str, if_missing=h.gen_message_id)

    # Classify notifications
    neighborhood_id = ForeignIdProperty(
        'Neighborhood', if_missing=lambda: c.project.neighborhood._id)
    project_id = ForeignIdProperty('Project', if_missing=lambda: c.project._id)
    app_config_id = ForeignIdProperty(
        'AppConfig', if_missing=lambda: c.app.config._id)
    tool_name = FieldProperty(str, if_missing=lambda: c.app.config.tool_name)
    ref_id = ForeignIdProperty('ArtifactReference')
    topic = FieldProperty(str)

    # Notification Content
    in_reply_to = FieldProperty(str)
    references = FieldProperty([str])
    from_address = FieldProperty(str)
    reply_to_address = FieldProperty(str)
    subject = FieldProperty(str)
    text = FieldProperty(str)
    link = FieldProperty(str)
    author_id = AlluraUserProperty()
    feed_meta = FieldProperty(S.Deprecated)
    artifact_reference = FieldProperty(S.Deprecated)
    pubdate = FieldProperty(datetime, if_missing=datetime.utcnow)

    ref = RelationProperty('ArtifactReference')

    view = jinja2.Environment(
        loader=jinja2.PackageLoader('allura', 'templates'),
        auto_reload=asbool(config.get('auto_reload_templates', True)),
    )

    @classmethod
    def post(cls, artifact, topic, **kw):
        '''Create a notification and  send the notify message'''
        n = cls._make_notification(artifact, topic, **kw)
        if n:
            # make sure notification is flushed in time for task to process it
            session(n).flush(n)
            n.fire_notification_task(artifact, topic)
        return n

    def fire_notification_task(self, artifact, topic):
        import allura.tasks.notification_tasks
        allura.tasks.notification_tasks.notify.post(
            self._id, artifact.index_id(), topic)

    @classmethod
    def post_user(cls, user, artifact, topic, **kw):
        '''Create a notification and deliver directly to a user's flash
    mailbox'''
        try:
            mbox = Mailbox(user_id=user._id, is_flash=True,
                           project_id=None,
                           app_config_id=None)
            session(mbox).flush(mbox)
        except pymongo.errors.DuplicateKeyError:
            session(mbox).expunge(mbox)
            mbox = Mailbox.query.get(user_id=user._id, is_flash=True)
        n = cls._make_notification(artifact, topic, **kw)
        if n:
            mbox.queue.append(n._id)
            mbox.queue_empty = False
        return n

    @classmethod
    def _make_notification(cls, artifact, topic, **kwargs):
        '''
        Create a Notification instance based on an artifact.  Special handling
        for comments when topic=='message'
        '''

        from allura.model import Project
        idx = artifact.index() if artifact else None
        subject_prefix = '[%s:%s] ' % (
            c.project.shortname, c.app.config.options.mount_point)
        post = ''
        if topic == 'message':
            post = kwargs.pop('post')
            text = kwargs.get('text') or post.text
            file_info = kwargs.pop('file_info', None)
            if file_info is not None:
                text = "%s\n\n\nAttachments:\n" % text
                if not isinstance(file_info, list):
                    file_info = [file_info]
                for attach in file_info:
                    attach.file.seek(0, 2)
                    bytecount = attach.file.tell()
                    attach.file.seek(0)
                    url = h.absurl('{}attachment/{}'.format(
                        post.url(), h.urlquote(attach.filename)))
                    text = "%s\n- [%s](%s) (%s; %s)" % (
                        text, attach.filename, url,
                        h.do_filesizeformat(bytecount), attach.type)

            subject = post.subject or ''
            if post.parent_id and not subject.lower().startswith('re:'):
                subject = 'Re: ' + subject
            author = post.author()
            msg_id = kwargs.get('message_id') or artifact.url() + post._id
            parent_msg_id = artifact.url() + \
                post.parent_id if post.parent_id else artifact.message_id()
            d = dict(
                _id=msg_id,
                from_address=str(
                    author._id) if author != User.anonymous() else None,
                reply_to_address='"%s" <%s>' % (
                    subject_prefix, getattr(
                        artifact, 'email_address', g.noreply)),
                subject=subject_prefix + subject,
                text=text,
                in_reply_to=parent_msg_id,
                references=cls._references(artifact, post),
                author_id=author._id,
                pubdate=datetime.utcnow())
        elif topic == 'flash':
            n = cls(topic=topic,
                    text=kwargs['text'],
                    subject=kwargs.pop('subject', ''))
            return n
        else:
            subject = kwargs.pop('subject', '%s modified by %s' % (
                h.get_first(idx, 'title'), c.user.get_pref('display_name')))
            reply_to = '"%s" <%s>' % (
                h.get_first(idx, 'title'),
                getattr(artifact, 'email_address', g.noreply))
            d = dict(
                from_address=reply_to,
                reply_to_address=reply_to,
                subject=subject_prefix + subject,
                text=kwargs.pop('text', subject),
                author_id=c.user._id,
                pubdate=datetime.utcnow())
            if kwargs.get('message_id'):
                d['_id'] = kwargs['message_id']
            if c.user.get_pref('email_address'):
                d['from_address'] = '"%s" <%s>' % (
                    c.user.get_pref('display_name'),
                    c.user.get_pref('email_address'))
            elif c.user.email_addresses:
                d['from_address'] = '"%s" <%s>' % (
                    c.user.get_pref('display_name'),
                    c.user.email_addresses[0])
        if not d.get('text'):
            d['text'] = ''
        try:
            ''' Add addional text to the notification e-mail based on the artifact type '''
            template = cls.view.get_template(
                'mail/' + artifact.type_s + '.txt')
            d['text'] += template.render(dict(c=c, g=g,
                                         config=config, data=artifact, post=post, h=h))
        except jinja2.TemplateNotFound:
            pass
        except:
            ''' Catch any errors loading or rendering the template,
            but the notification still gets sent if there is an error
            '''
            log.warn('Could not render notification template %s' %
                     artifact.type_s, exc_info=True)

        assert d['reply_to_address'] is not None
        project = c.project
        if d.get('project_id', c.project._id) != c.project._id:
            project = Project.query.get(_id=d['project_id'])
        if project.notifications_disabled:
            log.debug(
                'Notifications disabled for project %s, not sending %s(%r)',
                project.shortname, topic, artifact)
            return None
        n = cls(ref_id=artifact.index_id(),
                topic=topic,
                link=kwargs.pop('link', artifact.url()),
                **d)
        return n

    def footer(self, toaddr=''):
        return self.ref.artifact.get_mail_footer(self, toaddr)

    def _sender(self):
        from allura.model import AppConfig
        app_config = AppConfig.query.get(_id=self.app_config_id)
        app = app_config.project.app_instance(app_config)
        return app.email_address if app else None

    @classmethod
    def _references(cls, artifact, post):
        msg_ids = []
        while post and post.parent_id:
            msg_ids.append(artifact.url() + post.parent_id)
            post = post.parent
        msg_ids.append(artifact.message_id())
        msg_ids.reverse()
        return msg_ids

    def send_simple(self, toaddr):
        allura.tasks.mail_tasks.sendsimplemail.post(
            toaddr=toaddr,
            fromaddr=self.from_address,
            reply_to=self.reply_to_address,
            subject=self.subject,
            sender=self._sender(),
            message_id=self._id,
            in_reply_to=self.in_reply_to,
            references=self.references,
            text=(self.text or '') + self.footer(toaddr))

    def send_direct(self, user_id):
        user = User.query.get(_id=ObjectId(user_id), disabled=False, pending=False)
        artifact = self.ref.artifact
        log.debug('Sending direct notification %s to user %s',
                  self._id, user_id)
        # Don't send if user disabled
        if not user:
            log.debug("Skipping notification - enabled user %s not found" %
                      user_id)
            return
        # Don't send if user doesn't have read perms to the artifact
        if user and artifact and \
                not security.has_access(artifact, 'read', user)():
            log.debug("Skipping notification - User %s doesn't have read "
                      "access to artifact %s" % (user_id, str(self.ref_id)))
            log.debug("User roles [%s]; artifact ACL [%s]; PSC ACL [%s]",
                      ', '.join([str(r) for r in security.Credentials.get().user_roles(
                          user_id=user_id, project_id=artifact.project._id).reaching_ids]),
                      ', '.join([str(a) for a in artifact.acl]),
                      ', '.join([str(a) for a in artifact.parent_security_context().acl]))
            return
        allura.tasks.mail_tasks.sendmail.post(
            destinations=[str(user_id)],
            fromaddr=self.from_address,
            reply_to=self.reply_to_address,
            subject=self.subject,
            message_id=self._id,
            in_reply_to=self.in_reply_to,
            references=self.references,
            sender=self._sender(),
            text=(self.text or '') + self.footer())

    @classmethod
    def send_digest(self, user_id, from_address, subject, notifications,
                    reply_to_address=None):
        if not notifications:
            return
        user = User.query.get(_id=ObjectId(user_id), disabled=False, pending=False)
        if not user:
            log.debug("Skipping notification - enabled user %s not found " %
                      user_id)
            return
        # Filter out notifications for which the user doesn't have read
        # permissions to the artifact.
        artifact = self.ref.artifact

        def perm_check(notification):
            return not (user and artifact) or \
                security.has_access(artifact, 'read', user)()
        notifications = filter(perm_check, notifications)

        log.debug('Sending digest of notifications [%s] to user %s', ', '.join(
            [n._id for n in notifications]), user_id)
        if reply_to_address is None:
            reply_to_address = from_address
        text = ['Digest of %s' % subject]
        for n in notifications:
            text.append('From: %s' % n.from_address)
            text.append('Subject: %s' % (n.subject or '(no subject)'))
            text.append('Message-ID: %s' % n._id)
            text.append('')
            text.append(n.text or '-no text-')
        text.append(n.footer())
        text = '\n'.join(text)
        allura.tasks.mail_tasks.sendmail.post(
            destinations=[str(user_id)],
            fromaddr=from_address,
            reply_to=reply_to_address,
            subject=subject,
            message_id=h.gen_message_id(),
            text=text)

    @classmethod
    def send_summary(self, user_id, from_address, subject, notifications):
        if not notifications:
            return
        log.debug('Sending summary of notifications [%s] to user %s', ', '.join(
            [n._id for n in notifications]), user_id)
        text = ['Digest of %s' % subject]
        for n in notifications:
            text.append('From: %s' % n.from_address)
            text.append('Subject: %s' % (n.subject or '(no subject)'))
            text.append('Message-ID: %s' % n._id)
            text.append('')
            text.append(h.text.truncate(n.text or '-no text-', 128))
        text.append(n.footer())
        text = '\n'.join(text)
        allura.tasks.mail_tasks.sendmail.post(
            destinations=[str(user_id)],
            fromaddr=from_address,
            reply_to=from_address,
            subject=subject,
            message_id=h.gen_message_id(),
            text=text)


class Mailbox(MappedClass):

    '''
    Holds a queue of notifications for an artifact, or a user (webflash messages)
    for a subscriber.
    FIXME: describe the Mailbox concept better.
    '''

    class __mongometa__:
        session = main_orm_session
        name = 'mailbox'
        unique_indexes = [
            ('user_id', 'project_id', 'app_config_id',
             'artifact_index_id', 'topic', 'is_flash'),
        ]
        indexes = [
            ('project_id', 'artifact_index_id'),
            ('is_flash', 'user_id'),
            ('type', 'next_scheduled'),  # for q_digest
            ('type', 'queue_empty'),  # for q_direct
            # for deliver()
            ('project_id', 'app_config_id', 'artifact_index_id', 'topic'),
        ]

    _id = FieldProperty(S.ObjectId)
    user_id = AlluraUserProperty(if_missing=lambda: c.user._id)
    project_id = ForeignIdProperty('Project', if_missing=lambda: c.project._id)
    app_config_id = ForeignIdProperty(
        'AppConfig', if_missing=lambda: c.app.config._id)

    # Subscription filters
    artifact_title = FieldProperty(str)
    artifact_url = FieldProperty(str)
    artifact_index_id = FieldProperty(str)
    topic = FieldProperty(str)

    # Subscription type
    is_flash = FieldProperty(bool, if_missing=False)
    type = FieldProperty(S.OneOf('direct', 'digest', 'summary', 'flash'))
    frequency = FieldProperty(dict(
        n=int, unit=S.OneOf('day', 'week', 'month')))
    next_scheduled = FieldProperty(datetime, if_missing=datetime.utcnow)
    last_modified = FieldProperty(datetime, if_missing=datetime(2000, 1, 1))

    # a list of notification _id values
    queue = FieldProperty([str])
    queue_empty = FieldProperty(bool)

    project = RelationProperty('Project')
    app_config = RelationProperty('AppConfig')

    @classmethod
    def subscribe(
            cls,
            user_id=None, project_id=None, app_config_id=None,
            artifact=None, topic=None,
            type='direct', n=1, unit='day'):
        if user_id is None:
            user_id = c.user._id
        if project_id is None:
            project_id = c.project._id
        if app_config_id is None:
            app_config_id = c.app.config._id
        tool_already_subscribed = cls.query.get(user_id=user_id,
                                                project_id=project_id,
                                                app_config_id=app_config_id,
                                                artifact_index_id=None)
        if tool_already_subscribed:
            return
        if artifact is None:
            artifact_title = 'All artifacts'
            artifact_url = None
            artifact_index_id = None
        else:
            i = artifact.index()
            artifact_title = h.get_first(i, 'title')
            artifact_url = artifact.url()
            artifact_index_id = i['id']
            artifact_already_subscribed = cls.query.get(user_id=user_id,
                                                        project_id=project_id,
                                                        app_config_id=app_config_id,
                                                        artifact_index_id=artifact_index_id)
            if artifact_already_subscribed:
                return
        d = dict(
            user_id=user_id, project_id=project_id, app_config_id=app_config_id,
            artifact_index_id=artifact_index_id, topic=topic)
        sess = session(cls)
        try:
            mbox = cls(
                type=type, frequency=dict(n=n, unit=unit),
                artifact_title=artifact_title,
                artifact_url=artifact_url,
                **d)
            sess.flush(mbox)
        except pymongo.errors.DuplicateKeyError:
            sess.expunge(mbox)
            mbox = cls.query.get(**d)
            mbox.artifact_title = artifact_title
            mbox.artifact_url = artifact_url
            mbox.type = type
            mbox.frequency.n = n
            mbox.frequency.unit = unit
            sess.flush(mbox)
        if not artifact_index_id:
            # Unsubscribe from individual artifacts when subscribing to the
            # tool
            for other_mbox in cls.query.find(dict(
                    user_id=user_id, project_id=project_id, app_config_id=app_config_id)):
                if other_mbox is not mbox:
                    other_mbox.delete()

    @classmethod
    def unsubscribe(
            cls,
            user_id=None, project_id=None, app_config_id=None,
            artifact_index_id=None, topic=None):
        if user_id is None:
            user_id = c.user._id
        if project_id is None:
            project_id = c.project._id
        if app_config_id is None:
            app_config_id = c.app.config._id
        cls.query.remove(dict(
            user_id=user_id,
            project_id=project_id,
            app_config_id=app_config_id,
            artifact_index_id=artifact_index_id,
            topic=topic))

    @classmethod
    def subscribed(
            cls, user_id=None, project_id=None, app_config_id=None,
            artifact=None, topic=None):
        if user_id is None:
            user_id = c.user._id
        if project_id is None:
            project_id = c.project._id
        if app_config_id is None:
            app_config_id = c.app.config._id
        if artifact is None:
            artifact_index_id = None
        else:
            i = artifact.index()
            artifact_index_id = i['id']
        return cls.query.find(dict(
            user_id=user_id,
            project_id=project_id,
            app_config_id=app_config_id,
            artifact_index_id=artifact_index_id)).count() != 0

    @classmethod
    def deliver(cls, nid, artifact_index_id, topic):
        '''Called in the notification message handler to deliver notification IDs
        to the appropriate mailboxes.  Atomically appends the nids
        to the appropriate mailboxes.
        '''
        d = {
            'project_id': c.project._id,
            'app_config_id': c.app.config._id,
            'artifact_index_id': {'$in': [None, artifact_index_id]},
            'topic': {'$in': [None, topic]}
        }
        mboxes = cls.query.find(d).all()
        log.debug('Delivering notification %s to mailboxes [%s]', nid, ', '.join(
            [str(m._id) for m in mboxes]))
        for mbox in mboxes:
            try:
                mbox.query.update(
                    {'$push': dict(queue=nid),
                     '$set': dict(last_modified=datetime.utcnow(),
                                  queue_empty=False),
                     })
                # Make sure the mbox doesn't stick around to be flush()ed
                session(mbox).expunge(mbox)
            except:
                # log error but try to keep processing, lest all the other eligible
                # mboxes for this notification get skipped and lost forever
                log.exception(
                    'Error adding notification: %s for artifact %s on project %s to user %s',
                    nid, artifact_index_id, c.project._id, mbox.user_id)

    @classmethod
    def fire_ready(cls):
        '''Fires all direct subscriptions with notifications as well as
        all summary & digest subscriptions with notifications that are ready.
        Clears the mailbox queue.
        '''
        now = datetime.utcnow()
        # Queries to find all matching subscription objects
        q_direct = dict(
            type='direct',
            queue_empty=False,
        )
        if MAILBOX_QUIESCENT:
            q_direct['last_modified'] = {'$lt': now - MAILBOX_QUIESCENT}
        q_digest = dict(
            type={'$in': ['digest', 'summary']},
            next_scheduled={'$lt': now})

        def find_and_modify_direct_mbox():
            return cls.query.find_and_modify(
                query=q_direct,
                update={'$set': dict(
                    queue=[],
                    queue_empty=True,
                )},
                new=False)

        for mbox in take_while_true(find_and_modify_direct_mbox):
            try:
                mbox.fire(now)
            except:
                log.exception(
                    'Error firing mbox: %s with queue: [%s]', str(mbox._id), ', '.join(mbox.queue))
                # re-raise so we don't keep (destructively) trying to process
                # mboxes
                raise

        for mbox in cls.query.find(q_digest):
            next_scheduled = now
            if mbox.frequency.unit == 'day':
                next_scheduled += timedelta(days=mbox.frequency.n)
            elif mbox.frequency.unit == 'week':
                next_scheduled += timedelta(days=7 * mbox.frequency.n)
            elif mbox.frequency.unit == 'month':
                next_scheduled += timedelta(days=30 * mbox.frequency.n)
            mbox = cls.query.find_and_modify(
                query=dict(_id=mbox._id),
                update={'$set': dict(
                        next_scheduled=next_scheduled,
                        queue=[],
                        queue_empty=True,
                        )},
                new=False)
            mbox.fire(now)

    def fire(self, now):
        '''
        Send all notifications that this mailbox has enqueued.
        '''
        notifications = Notification.query.find(dict(_id={'$in': self.queue}))
        notifications = notifications.all()
        if len(notifications) != len(self.queue):
            log.error('Mailbox queue error: Mailbox %s queued [%s], found [%s]', str(
                self._id), ', '.join(self.queue), ', '.join([n._id for n in notifications]))
        else:
            log.debug('Firing mailbox %s notifications [%s], found [%s]', str(
                self._id), ', '.join(self.queue), ', '.join([n._id for n in notifications]))
        if self.type == 'direct':
            ngroups = defaultdict(list)
            for n in notifications:
                try:
                    if n.topic == 'message':
                        n.send_direct(self.user_id)
                        # Messages must be sent individually so they can be replied
                        # to individually
                    else:
                        key = (n.subject, n.from_address,
                               n.reply_to_address, n.author_id)
                        ngroups[key].append(n)
                except:
                    # log error but keep trying to deliver other notifications,
                    # lest the other notifications (which have already been removed
                    # from the mobx's queue in mongo) be lost
                    log.exception(
                        'Error sending notification: %s to mbox %s (user %s)',
                        n._id, self._id, self.user_id)
            # Accumulate messages from same address with same subject
            for (subject, from_address, reply_to_address, author_id), ns in ngroups.iteritems():
                try:
                    if len(ns) == 1:
                        ns[0].send_direct(self.user_id)
                    else:
                        Notification.send_digest(
                            self.user_id, from_address, subject, ns, reply_to_address)
                except:
                    # log error but keep trying to deliver other notifications,
                    # lest the other notifications (which have already been removed
                    # from the mobx's queue in mongo) be lost
                    log.exception(
                        'Error sending notifications: [%s] to mbox %s (user %s)',
                        ', '.join([n._id for n in ns]), self._id, self.user_id)
        elif self.type == 'digest':
            Notification.send_digest(
                self.user_id, g.noreply, 'Digest Email',
                notifications)
        elif self.type == 'summary':
            Notification.send_summary(
                self.user_id, g.noreply, 'Digest Email',
                notifications)


class MailFooter(object):
    view = jinja2.Environment(
        loader=jinja2.PackageLoader('allura', 'templates'),
        auto_reload=asbool(config.get('auto_reload_templates', True)),
    )

    @classmethod
    def _render(cls, template, **kw):
        return cls.view.get_template(template).render(kw)

    @classmethod
    def standard(cls, notification, allow_email_posting=True, **kw):
        return cls._render('mail/footer.txt',
                           domain=config['domain'],
                           notification=notification,
                           prefix=config['forgemail.url'],
                           allow_email_posting=allow_email_posting,
                           **kw)

    @classmethod
    def monitored(cls, toaddr, app_url, setting_url):
        return cls._render('mail/monitor_email_footer.txt',
                           domain=config['domain'],
                           email=toaddr,
                           app_url=app_url,
                           setting_url=setting_url)


class SiteNotification(MappedClass):

    """
    Storage for site-wide notification.
    """

    class __mongometa__:
        session = main_orm_session
        name = 'site_notification'

    _id = FieldProperty(S.ObjectId)
    content = FieldProperty(str, if_missing='')
    active = FieldProperty(bool, if_missing=True)
    impressions = FieldProperty(
        int, if_missing=lambda: config.get('site_notification.impressions', 0))

    @classmethod
    def current(cls):
        note = cls.query.find().sort('_id', -1).limit(1).first()
        if note is None or not note.active:
            return None
        return note
