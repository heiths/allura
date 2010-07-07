import re
import logging
import string
from urllib import quote
from ConfigParser import RawConfigParser
from pprint import pformat

from tg import config
from pylons import g, request
from BeautifulSoup import BeautifulSoup

import oembed
import markdown
import feedparser

from . import macro

log = logging.getLogger(__name__)

class ForgeExtension(markdown.Extension):
    core_artifact_link = r'(\[((?P<project_id>.*?):)?((?P<app_id>.*?):)?(?P<artifact_id>.*?)\])'

    def __init__(self, wiki=False):
        markdown.Extension.__init__(self)
        self._use_wiki = wiki

    def extendMarkdown(self, md, md_globals):
        macro_engine = Macro(md)
        md.preprocessors.add('macro', macro_engine.preprocessor, '<html_block')
        md.treeprocessors['br'] = LineOrientedTreeProcessor(md)
        md.inlinePatterns['oembed'] = OEmbedPattern(r'\[embed#(.*?)\]')
        md.inlinePatterns['autolink_1'] = AutolinkPattern(r'(http(?:s?)://[a-zA-Z0-9./\-_0%?&=+]+)')
        md.inlinePatterns['artifact'] = ArtifactLinkPattern(
            self.core_artifact_link, md, self._use_wiki)
        if self._use_wiki:
            md.treeprocessors['wiki'] = ClassifyWikiLinks(md)
        md.postprocessors['macro'] = macro_engine.postprocessor
        md.postprocessors['sanitize_html'] = HTMLSanitizer()
        md.postprocessors['rewrite_relative_links'] = RelativeLinkRewriter()

class RelativeLinkRewriter(markdown.postprocessors.Postprocessor):

    def run(self, text):
        try:
            if not request.path_info.endswith('/'): return text
        except:
            # Must be being called outside the request context
            pass
        soup = BeautifulSoup(text)
        def rewrite(tag, attr):
            val = tag.get(attr)
            if val is None: return
            if '://' in val: return
            if val.startswith('/'): return
            if val.startswith('.'): return
            tag[attr] = '../' + val
        for link in soup.findAll('a'):
            rewrite(link, 'href')
        for link in soup.findAll('img'):
            rewrite(link, 'src')
        return unicode(soup)

class HTMLSanitizer(markdown.postprocessors.Postprocessor):

    def run(self, text):
        try:
            p = feedparser._HTMLSanitizer('utf-8')
        except TypeError: # $@%## pre-released versions from SOG
            p = feedparser._HTMLSanitizer('utf-8', '')
        p.feed(text.encode('utf-8'))
        return unicode(p.output(), 'utf-8')

class LineOrientedTreeProcessor(markdown.treeprocessors.Treeprocessor):
    '''Once MD is satisfied with the etree, this runs to replace \n with <br/>
    within <p>s.
    '''

    def __init__(self, md):
        self._markdown = md
    
    def run(self, root):
        for node in root.getiterator('p'):
            if not node.text: continue
            if '\n' not in node.text: continue
            text = self._markdown.serializer(node)
            text = self._markdown.postprocessors['raw_html'].run(text)
            text = text.strip()
            if '\n' not in text: continue
            new_text = (text
                        .replace('<br>', '<br/>')
                        .replace('\n', '<br/>'))
            try:
                new_node = markdown.etree.fromstring(new_text)
                node.clear()
                node.text = new_node.text
                node[:] = list(new_node)
            except SyntaxError:
                log.exception('Error adding <br> tags: new text is %s', new_text)
                pass
        return root

class ClassifyWikiLinks(markdown.treeprocessors.Treeprocessor):
    '''This adds a class to intra-wiki links that point to non-existent pages'''

    def __init__(self, md):
        self._markdown = md

    def run(self, root):
        from pyforge import model as M
        for node in root.getiterator('a'):
            href = node.get('href')
            if not href: continue
            if '/' in href: continue
            al = M.ArtifactLink.lookup('[' + href + ']')
            if not al:
                classes = node.get('class', '').split() + ['notfound']
                node.attrib['class'] = ' '.join(classes)
        return root

class ArtifactLinkPattern(markdown.inlinepatterns.LinkPattern):

    def __init__(self, pattern, markdown_instance=None, use_wiki=False):
        markdown.inlinepatterns.LinkPattern.__init__(self, pattern, markdown_instance)
        self._use_wiki = use_wiki

    def handleMatch(self, mo):
        from pyforge import model as M
        old_link = mo.group(2)
        if old_link.startswith('[['): return old_link
        new_link = M.ArtifactLink.lookup(old_link)
        if new_link:
            result = markdown.etree.Element('a')
            result.text = old_link
            result.set('href', new_link.url)
            return result
        elif self._use_wiki:
            result = markdown.etree.Element('a')
            result.text = old_link
            result.set('href', old_link[1:-1])
            return result
        else:
            return old_link

class AutolinkPattern(markdown.inlinepatterns.LinkPattern):

    def handleMatch(self, mo):
        old_link = mo.group(2)
        result = markdown.etree.Element('a')
        result.text = old_link
        result.set('href', old_link)
        return result

class WikiLinkPattern(markdown.inlinepatterns.LinkPattern):

    def handleMatch(self, mo):
        old_link = mo.group(2)
        result = markdown.etree.Element('a')
        result.text = old_link
        result.set('href', '../' + old_link + '/')
        return result

class OEmbedPattern(markdown.inlinepatterns.LinkPattern):

    def handleMatch(self, mo):
        href = mo.group(2)
        try:
            if href.startswith('('):
                size, href = href.split(')')
                size = size[1:]
                width,height = size.split(',')
            else:
                width = height = None
            response = g.oembed_consumer.embed(href)
            data = response.getData()
            log.info('Got response:\n%s', pformat(data))
            if width is None: width = data.get('width', '100%')
            if height is None: height = data.get('height', '300')
            return self._render_iframe(href, width, height)
        except oembed.OEmbedNoEndpoint:
            result = markdown.etree.Element('a')
            result.text = href + ' (cannot be embedded - no endpoint)'
            result.set('href', href)
            return result
        except Exception, ve:
            result = markdown.etree.Element('a')
            result.text = href + ' (cannot be embedded (%s)' % ve
            result.set('href', href)
            return result

    def _render_iframe(self, href, width, height):
        iframe = markdown.etree.Element('iframe')
        href = 'http://%s?href=%s' % (
            config['oembed.host'], quote(href))
        iframe.set('src', href)
        iframe.set('width', str(width))
        iframe.set('height', str(height))
        return iframe

class Macro(object):
    macro_re_text = r'\[\[(.*?)\]\]'
    macro_re = re.compile(macro_re_text)

    def __init__(self, md):
        self.preprocessor = MacroPreprocessor(md, self)
        self.postprocessor = MacroPostprocessor(md, self)
        self.macros = {}

    def register(self, text):
        k = '-%d-' % len(self.macros)
        self.macros[k] = text
        return k

class MacroPreprocessor(markdown.preprocessors.Preprocessor):
    '''Strip macros from the incoming text and save them for later'''

    def __init__(self, md, macro):
        markdown.preprocessors.Preprocessor.__init__(self, md)
        self.macro = macro

    def run(self, lines):
        def repl(mo):
            macro_text = mo.group(1)
            if macro_text.startswith('\\'):
                macro_text = 'quote ' + macro_text[1:]
            placeholder = self.macro.register(macro_text)
            return '[[%s]]' % placeholder
        return [ self.macro.macro_re.sub(repl,line)
                 for line in lines ]

class MacroPostprocessor(markdown.postprocessors.Postprocessor):
    '''Expand macro placeholders'''

    def __init__(self, md, macro):
        markdown.postprocessors.Postprocessor.__init__(self, md)
        self.macro = macro
        
    def run(self, text):
        def repl(mo):
            macro_text = self.macro.macros[mo.group(1)]
            result = macro.parse(macro_text)
            return result
        return self.macro.macro_re.sub(repl, text)

