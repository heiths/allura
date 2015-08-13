# -*- coding: utf-8 -*-

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


"""The application's Globals object"""

__all__ = ['Globals']
import logging
import cgi
import hashlib
import json
import datetime
from urllib import urlencode
from subprocess import Popen, PIPE
import os
import time
import traceback

import activitystream
import pkg_resources
import markdown
import pygments
import pygments.lexers
import pygments.formatters
import pygments.util
from tg import config, session
from pylons import request
from pylons import tmpl_context as c
from paste.deploy.converters import asbool, asint, aslist
from pypeline.markup import markup as pypeline_markup

import ew as ew_core
import ew.jinja2_ew as ew
from ming.utils import LazyProperty
from jinja2 import Markup

import allura.tasks.event_tasks
from allura import model as M
from allura.lib.markdown_extensions import (
    ForgeExtension,
    CommitMessageExtension,
)
from allura.eventslistener import PostEvent

from allura.lib import gravatar, plugin, utils
from allura.lib import helpers as h
from allura.lib.widgets import analytics
from allura.lib.security import Credentials
from allura.lib.solr import MockSOLR, make_solr_from_config
from allura.model.session import artifact_orm_session

log = logging.getLogger(__name__)


class ForgeMarkdown(markdown.Markdown):

    def convert(self, source, render_limit=True):
        if render_limit and len(source) > asint(config.get('markdown_render_max_length', 40000)):
            # if text is too big, markdown can take a long time to process it,
            # so we return it as a plain text
            log.info('Text is too big. Skipping markdown processing')
            escaped = cgi.escape(h.really_unicode(source))
            return h.html.literal(u'<pre>%s</pre>' % escaped)
        try:
            return markdown.Markdown.convert(self, source)
        except Exception:
            log.info('Invalid markdown: %s  Upwards trace is %s', source,
                     ''.join(traceback.format_stack()), exc_info=True)
            escaped = h.really_unicode(source)
            escaped = cgi.escape(escaped)
            return h.html.literal(u"""<p><strong>ERROR!</strong> The markdown supplied could not be parsed correctly.
            Did you forget to surround a code snippet with "~~~~"?</p><pre>%s</pre>""" % escaped)

    def cached_convert(self, artifact, field_name):
        """Convert ``artifact.field_name`` markdown source to html, caching
        the result if the render time is greater than the defined threshold.

        """
        source_text = getattr(artifact, field_name)
        # Check if contents macro and never cache
        if "[[" in source_text:
            return self.convert(source_text)
        cache_field_name = field_name + '_cache'
        cache = getattr(artifact, cache_field_name, None)
        if not cache:
            log.warn(
                'Skipping Markdown caching - Missing cache field "%s" on class %s',
                field_name, artifact.__class__.__name__)
            return self.convert(source_text)

        bugfix_rev = 2  # increment this if we need all caches to invalidated (e.g. xss in markdown rendering fixed)
        md5 = None
        # If a cached version exists and it is valid, return it.
        if cache.md5 is not None:
            md5 = hashlib.md5(source_text.encode('utf-8')).hexdigest()
            if cache.md5 == md5 and getattr(cache, 'fix7528', False) == bugfix_rev:
                return h.html.literal(cache.html)

        # Convert the markdown and time the result.
        start = time.time()
        html = self.convert(source_text, render_limit=False)
        render_time = time.time() - start

        threshold = config.get('markdown_cache_threshold')
        try:
            threshold = float(threshold) if threshold else None
        except ValueError:
            threshold = None
            log.warn('Skipping Markdown caching - The value for config param '
                     '"markdown_cache_threshold" must be a float.')

        if threshold is not None and render_time > threshold:
            # Save the cache
            if md5 is None:
                md5 = hashlib.md5(source_text.encode('utf-8')).hexdigest()
            cache.md5, cache.html, cache.render_time = md5, html, render_time
            cache.fix7528 = bugfix_rev  # flag to indicate good caches created after [#7528] and other critical bugs were fixed.

            # Prevent cache creation from updating the mod_date timestamp.
            _session = artifact_orm_session._get()
            _session.skip_mod_date = True
        return html


class NeighborhoodCache(object):
    """Cached Neighborhood objects by url_prefix.
    For faster RootController.__init__ lookup
    """

    def __init__(self, duration):
        self.duration = duration
        self._data = {}

    def _lookup(self, url_prefix):
        n = M.Neighborhood.query.get(url_prefix=url_prefix)
        self._data[url_prefix] = {
            'object': n,
            'ts': datetime.datetime.utcnow(),
        }
        return n

    def _expired(self, n):
        delta = datetime.datetime.utcnow() - n['ts']
        if delta >= datetime.timedelta(seconds=self.duration):
            return True
        return False

    def get(self, url_prefix):
        n = self._data.get(url_prefix)
        if n and not self._expired(n):
            return n['object']
        return self._lookup(url_prefix)


class Globals(object):

    """Container for objects available throughout the life of the application.

    One instance of Globals is created during application initialization and
    is available during requests via the 'app_globals' variable.

    """
    __shared_state = {}

    def __init__(self):
        self.__dict__ = self.__shared_state
        if self.__shared_state:
            return
        self.allura_templates = pkg_resources.resource_filename(
            'allura', 'templates')
        # Setup SOLR
        self.solr_server = aslist(config.get('solr.server'), ',')
        # skip empty strings in case of extra commas
        self.solr_server = [s for s in self.solr_server if s]
        self.solr_query_server = config.get('solr.query_server')
        if self.solr_server:
            self.solr = make_solr_from_config(
                self.solr_server, self.solr_query_server)
            self.solr_short_timeout = make_solr_from_config(
                self.solr_server, self.solr_query_server,
                timeout=int(config.get('solr.short_timeout', 10)))
        else:  # pragma no cover
            log.warning('Solr config not set; using in-memory MockSOLR')
            self.solr = self.solr_short_timeout = MockSOLR()

        # Load login/logout urls; only used for customized logins
        self.login_url = config.get('auth.login_url', '/auth/')
        self.logout_url = config.get('auth.logout_url', '/auth/logout')
        self.login_fragment_url = config.get(
            'auth.login_fragment_url', '/auth/login_fragment/')

        # Setup Gravatar
        self.gravatar = gravatar.url

        # Setup pygments
        self.pygments_formatter = utils.LineAnchorCodeHtmlFormatter(
            cssclass='codehilite',
            linenos='table')

        # Setup Pypeline
        self.pypeline_markup = pypeline_markup

        # Setup analytics
        accounts = config.get('ga.account', 'UA-XXXXX-X')
        accounts = accounts.split(' ')
        self.analytics = analytics.GoogleAnalytics(accounts=accounts)

        self.icons = dict(
            move=Icon('fa fa-arrows', 'Move'),
            edit=Icon('fa fa-edit', 'Edit'),
            admin=Icon('fa fa-gear', 'Admin'),
            send=Icon('fa fa-send-o', 'Send'),
            add=Icon('fa fa-plus-circle', 'Add'),
            moderate=Icon('fa fa-hand-stop-o', 'Moderate'),
            pencil=Icon('fa fa-pencil', 'Edit'),
            help=Icon('fa fa-question-circle', 'Help'),
            eye=Icon('fa fa-eye', 'View'),
            search=Icon('fa fa-search', 'Search'),
            history=Icon('fa fa-calendar', 'History'),
            feed=Icon('fa fa-rss', 'Feed'),
            mail=Icon('fa fa-envelope-o', 'Subscribe'),
            reply=Icon('w', 'ico-reply'),
            tag=Icon('fa fa-tag', 'Tag'),
            flag=Icon('^', 'ico-flag'),
            undelete=Icon('fa fa-undo', 'Undelete'),
            delete=Icon('fa fa-trash-o', 'Delete'),
            close=Icon('D', 'ico-close'),
            table=Icon('n', 'ico-table'),
            stats=Icon('fa fa-line-chart', 'Stats'),
            pin=Icon('@', 'ico-pin'),
            folder=Icon('o', 'ico-folder'),
            fork=Icon('R', 'ico-fork'),
            merge=Icon('J', 'ico-merge'),
            plus=Icon('fa fa-plus-circle', 'Add'),
            conversation=Icon('fa fa-comments', 'Conversation'),
            group=Icon('g', 'ico-group'),
            user=Icon('U', 'ico-user'),
            secure=Icon('(', 'ico-lock'),
            unsecure=Icon(')', 'ico-unlock'),
            star=Icon('S', 'ico-star'),
            watch=Icon('E', 'ico-watch'),
            expand=Icon('`', 'ico-expand'),
            restore=Icon('J', 'ico-restore'),
            # Permissions
            perm_read=Icon('E', 'ico-focus'),
            perm_update=Icon('0', 'ico-sync'),
            perm_create=Icon('e', 'ico-config'),
            perm_register=Icon('e', 'ico-config'),
            perm_delete=Icon('-', 'ico-minuscirc'),
            perm_tool=Icon('x', 'ico-config'),
            perm_admin=Icon('(', 'ico-lock'),
            perm_has_yes=Icon('3', 'ico-check'),
            perm_has_no=Icon('d', 'ico-noentry'),
            perm_has_inherit=Icon('2', 'ico-checkcircle'),
        )

        # Cache some loaded entry points
        def _cache_eps(section_name, dict_cls=dict):
            d = dict_cls()
            for ep in h.iter_entry_points(section_name):
                value = ep.load()
                d[ep.name] = value
            return d

        class entry_point_loading_dict(dict):

            def __missing__(self, key):
                self[key] = _cache_eps(key)
                return self[key]

        self.entry_points = entry_point_loading_dict(
            tool=_cache_eps('allura', dict_cls=utils.CaseInsensitiveDict),
            auth=_cache_eps('allura.auth'),
            registration=_cache_eps('allura.project_registration'),
            theme=_cache_eps('allura.theme'),
            user_prefs=_cache_eps('allura.user_prefs'),
            spam=_cache_eps('allura.spam'),
            phone=_cache_eps('allura.phone'),
            stats=_cache_eps('allura.stats'),
            site_stats=_cache_eps('allura.site_stats'),
            admin=_cache_eps('allura.admin'),
            site_admin=_cache_eps('allura.site_admin'),
            # macro eps are used solely for ensuring that external macros are
            # imported (after load, the ep itself is not used)
            macros=_cache_eps('allura.macros'),
            webhooks=_cache_eps('allura.webhooks'),
        )

        # Neighborhood cache
        duration = asint(config.get('neighborhood.cache.duration', 0))
        self.neighborhood_cache = NeighborhoodCache(duration)

        # Set listeners to update stats
        statslisteners = []
        for name, ep in self.entry_points['stats'].iteritems():
            statslisteners.append(ep())
        self.statsUpdater = PostEvent(statslisteners)

        self.tmpdir = os.getenv('TMPDIR', '/tmp')

    @LazyProperty
    def spam_checker(self):
        """Return a SpamFilter implementation.
        """
        from allura.lib import spam
        return spam.SpamFilter.get(config, self.entry_points['spam'])

    @LazyProperty
    def phone_service(self):
        """Return a :class:`allura.lib.phone.PhoneService` implementation"""
        from allura.lib import phone
        return phone.PhoneService.get(config, self.entry_points['phone'])

    @LazyProperty
    def director(self):
        """Return activitystream director"""
        if asbool(config.get('activitystream.recording.enabled', False)):
            return activitystream.director()
        else:
            class NullActivityStreamDirector(object):

                def connect(self, *a, **kw):
                    pass

                def disconnect(self, *a, **kw):
                    pass

                def is_connected(self, *a, **kw):
                    return False

                def create_activity(self, *a, **kw):
                    pass

                def create_timeline(self, *a, **kw):
                    pass

                def create_timelines(self, *a, **kw):
                    pass

                def get_timeline(self, *a, **kw):
                    return []
            return NullActivityStreamDirector()

    def post_event(self, topic, *args, **kwargs):
        allura.tasks.event_tasks.event.post(topic, *args, **kwargs)

    @LazyProperty
    def theme(self):
        return plugin.ThemeProvider.get()

    @property
    def antispam(self):
        a = request.environ.get('allura.antispam')
        if a is None:
            a = request.environ['allura.antispam'] = utils.AntiSpam()
        return a

    @property
    def credentials(self):
        return Credentials.get()

    def handle_paging(self, limit, page, default=25):
        limit = self.manage_paging_preference(limit, default)
        page = max(int(page), 0)
        start = page * int(limit)
        return (limit, page, start)

    def manage_paging_preference(self, limit, default=25):
        if not limit:
            if c.user in (None, M.User.anonymous()):
                limit = default
            else:
                limit = c.user.get_pref('results_per_page') or default
        return int(limit)

    def document_class(self, neighborhood):
        classes = ''
        if neighborhood:
            classes += ' neighborhood-%s' % neighborhood.name
        if not neighborhood and c.project:
            classes += ' neighborhood-%s' % c.project.neighborhood.name
        if c.project:
            classes += ' project-%s' % c.project.shortname
        if c.app:
            classes += ' mountpoint-%s' % c.app.config.options.mount_point
        return classes

    def highlight(self, text, lexer=None, filename=None):
        if not text:
            return h.html.literal('<em>Empty file</em>')
        # Don't use line numbers for diff highlight's, as per [#1484]
        if lexer == 'diff':
            formatter = pygments.formatters.HtmlFormatter(
                cssclass='codehilite', linenos=False)
        else:
            formatter = self.pygments_formatter
        if lexer is None:
            try:
                lexer = pygments.lexers.get_lexer_for_filename(
                    filename, encoding='chardet')
            except pygments.util.ClassNotFound:
                # no highlighting, but we should escape, encode, and wrap it in
                # a <pre>
                text = h.really_unicode(text)
                text = cgi.escape(text)
                return h.html.literal(u'<pre>' + text + u'</pre>')
        else:
            lexer = pygments.lexers.get_lexer_by_name(
                lexer, encoding='chardet')
        return h.html.literal(pygments.highlight(text, lexer, formatter))

    def forge_markdown(self, **kwargs):
        '''return a markdown.Markdown object on which you can call convert'''
        return ForgeMarkdown(
            # 'fenced_code'
            extensions=['codehilite',
                        ForgeExtension(
                            **kwargs), 'tables', 'toc', 'nl2br'],
            output_format='html4')

    @property
    def markdown(self):
        return self.forge_markdown()

    @property
    def markdown_wiki(self):
        if c.project.is_nbhd_project:
            return self.forge_markdown(wiki=True, macro_context='neighborhood-wiki')
        elif c.project.is_user_project:
            return self.forge_markdown(wiki=True, macro_context='userproject-wiki')
        else:
            return self.forge_markdown(wiki=True)

    @property
    def markdown_commit(self):
        """Return a Markdown parser configured for rendering commit messages.

        """
        app = getattr(c, 'app', None)
        return ForgeMarkdown(extensions=[CommitMessageExtension(app), 'nl2br'],
                             output_format='html4')

    @property
    def production_mode(self):
        return asbool(config.get('debug')) == False

    @LazyProperty
    def user_message_time_interval(self):
        """The rolling window of time (in seconds) during which no more than
        :meth:`user_message_max_messages` may be sent by any one user.

        """
        return int(config.get('user_message.time_interval', 3600))

    @LazyProperty
    def user_message_max_messages(self):
        """The number of user messages that can be sent within
        meth:`user_message_time_interval` before rate-limiting is enforced.

        """
        return int(config.get('user_message.max_messages', 20))

    @LazyProperty
    def server_name(self):
        p1 = Popen(['hostname', '-s'], stdout=PIPE)
        server_name = p1.communicate()[0].strip()
        p1.wait()
        return server_name

    @property
    def tool_icon_css(self):
        """Return a (css, md5) tuple, where ``css`` is a string of CSS
        containing class names and icon urls for every installed tool, and
        ``md5`` is the md5 hexdigest of ``css``.

        """
        css = ''
        for tool_name in self.entry_points['tool']:
            for size in (24, 32, 48):
                url = self.theme.app_icon_url(tool_name.lower(), size)
                css += '.ui-icon-tool-%s-%i {background: url(%s) no-repeat;}\n' % (
                    tool_name, size, url)
        return css, hashlib.md5(css).hexdigest()

    @property
    def resource_manager(self):
        return ew_core.widget_context.resource_manager

    def register_css(self, href, **kw):
        self.resource_manager.register(ew.CSSLink(href, **kw))

    def register_js(self, href, **kw):
        self.resource_manager.register(ew.JSLink(href, **kw))

    def register_forge_css(self, href, **kw):
        self.resource_manager.register(ew.CSSLink('allura/' + href, **kw))

    def register_forge_js(self, href, **kw):
        self.resource_manager.register(ew.JSLink('allura/' + href, **kw))

    def register_app_css(self, href, **kw):
        app = kw.pop('app', c.app)
        self.resource_manager.register(
            ew.CSSLink('tool/%s/%s' % (app.config.tool_name.lower(), href), **kw))

    def register_app_js(self, href, **kw):
        app = kw.pop('app', c.app)
        self.resource_manager.register(
            ew.JSLink('tool/%s/%s' % (app.config.tool_name.lower(), href), **kw))

    def register_theme_css(self, href, **kw):
        self.resource_manager.register(ew.CSSLink(self.theme_href(href), **kw))

    def register_theme_js(self, href, **kw):
        self.resource_manager.register(ew.JSLink(self.theme_href(href), **kw))

    def register_js_snippet(self, text, **kw):
        self.resource_manager.register(ew.JSScript(text, **kw))

    def theme_href(self, href):
        return self.theme.href(href)

    def forge_static(self, resource):
        base = config['static.url_base']
        if base.startswith(':'):
            base = request.scheme + base
        return base + resource

    def app_static(self, resource, app=None):
        base = config['static.url_base']
        app = app or c.app
        if base.startswith(':'):
            base = request.scheme + base
        return (base + app.config.tool_name.lower() + '/' + resource)

    def set_project(self, pid_or_project):
        'h.set_context() is preferred over this method'
        if isinstance(pid_or_project, M.Project):
            c.project = pid_or_project
        elif isinstance(pid_or_project, basestring):
            raise TypeError('need a Project instance, got %r' % pid_or_project)
        elif pid_or_project is None:
            c.project = None
        else:
            c.project = None
            log.error('Trying g.set_project(%r)', pid_or_project)

    def set_app(self, name):
        'h.set_context() is preferred over this method'
        c.app = c.project.app_instance(name)

    def url(self, base, **kw):
        params = urlencode(kw)
        if params:
            return '%s%s?%s' % (request.host_url, base, params)
        else:
            return '%s%s' % (request.host_url, base)

    def postload_contents(self):
        text = '''
'''
        return json.dumps(dict(text=text))

    def year(self):
        return datetime.datetime.utcnow().year

    @LazyProperty
    def noreply(self):
        return unicode(config.get('noreply', 'noreply@%s' % config['domain']))

    @property
    def build_key(self):
        return config.get('build_key', '')


class Icon(object):

    def __init__(self, css, title=None):
        self.css = css
        self.title = title or u''

    def render(self, show_title=False, extra_css=None, closing_tag=True, **kw):
        title = kw.get('title') or self.title
        attrs = {
            'href': '#',
            'title': title,
            'class': ' '.join(['icon', self.css, extra_css or '']).strip(),
        }
        attrs.update(kw)
        attrs = ew._Jinja2Widget().j2_attrs(attrs)
        visible_title = u''
        if show_title:
            visible_title = u'<span>&nbsp;{}</span>'.format(Markup.escape(title))
        closing_tag = u'</a>' if closing_tag else u''
        icon = u'<a {}>{}{}'.format(attrs, visible_title, closing_tag)
        return Markup(icon)
