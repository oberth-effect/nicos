# *****************************************************************************
# NICOS, the Networked Instrument Control System of the MLZ
# Copyright (c) 2009-present by the NICOS contributors (see AUTHORS)
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
#
# Module authors:
#   Stepan Venclik <stepan.venclik@matfyz.cuni.cz>
#
# *****************************************************************************

"""Code assist for NICOS scripts in the GUI: the catalog of the connected
daemon as stub modules for jedi, and what jedi cannot know.

The catalog is the result of ``session.getNamespaceInfo()`` (see
:mod:`nicos.core.sessions.introspect`): the stub modules of the user
commands, the exported devices and their classes, and the classes reached
from these through type annotations, with some data for the hovers and the
suggestion lists.  :class:`Catalog` fetches it once per connection and setup
change and writes the stub modules into a directory of its own.  Prefixed
with :data:`HEADER`, a script is plain Python to jedi, which then resolves
the NICOS names together with the rest of the script: assignments, loops,
imports and the results of annotated methods.

The Monaco editor gets completion, hover and signature help from the
language server that qtmonaco runs, configured for the stub directory and
the header (see :mod:`nicos.clients.gui.widgets.monacoeditor`).
:class:`CodeAssist` adds for one front end what the language server does
not show: the type of a value in its hover, which jedi in this process
infers, the description and the current values of a device, and the members
of names that only the daemon knows, from the daemon's completer.  The input
line of a console completes members with jedi in this process, through the
callback that :func:`lineCompleter` returns.

Daemon requests: the fetch of the catalog (one ``eval``), the cached values
of a device for its hover (``getcachekeys``) and the daemon's completer
(``complete``).
"""

import atexit
import html
import os
import re
import shutil
import tempfile
import threading

from nicos.core.sessions.introspect import HEADER, SESSION_MODULE, TYPES_MODULE
from nicos.core.status import BUSY, DISABLED, ERROR, NOTREACHED, OK, WARN, \
    statuses
from nicos.guisupport.colors import colors
from nicos.guisupport.qt import QColor, QObject, QTimer, pyqtSignal

try:
    import jedi
except ImportError:
    jedi = None

CATALOG_DELAY = 500  # ms to collect setup/device events before a fetch

# the files of the stub modules
STUB_FILES = (SESSION_MODULE + '.py', TYPES_MODULE + '.py')

# a dotted name before the cursor, the kind of expression the daemon's
# completer takes, and the beginning of the member name
DOTTED_NAME = re.compile(r'(?<![\w.)\]])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)'
                         r'\.(\w*)$')

# jedi's caches are not thread safe
_jediLock = threading.Lock()


class Catalog(QObject):
    """The catalog of the daemon of a client, kept up to date, and its stub
    modules.

    It is fetched 500 ms after the last of the client events `connected`,
    `setup` and `device`, with one eval of ``session.getNamespaceInfo()``,
    and cleared when the client disconnects.  On every update the stub
    modules in :attr:`stubdir` are rewritten and :attr:`changed` is emitted.

    :attr:`commands` and :attr:`devices` map names to the command and device
    entries of the catalog, :attr:`apis` the device classes, as jedi names
    them, to their user API; :attr:`available` tells if it is not empty.
    """

    changed = pyqtSignal()

    def __init__(self, client, log):
        # not a child of the client: the GUI client never receives Qt events,
        # and the first one (ChildAdded) would hit a tuple named "event" that
        # a loop of its class body leaves behind, shadowing QObject.event
        QObject.__init__(self)
        self.client = client
        self.log = log
        self.stubdir = tempfile.mkdtemp(prefix='nicos-catalog-')
        atexit.register(shutil.rmtree, self.stubdir, True)
        self._warned = False
        self.update({})
        self._timer = QTimer(self, singleShot=True, interval=CATALOG_DELAY)
        self._timer.timeout.connect(self.refresh)
        for signal in (client.connected, client.setup, client.device):
            signal.connect(self.schedule)
        client.disconnected.connect(self.clear)
        if client.isconnected:
            self.schedule()

    def schedule(self, *args):
        self._timer.start()

    def clear(self):
        self._warned = False
        self.update({})

    def refresh(self):
        if not self.client.isconnected:
            return
        info = self.client.eval('session.getNamespaceInfo()', None)
        if isinstance(info, dict):
            self.update(info)
        elif not self._warned:
            self._warned = True
            self.log.warning('daemon does not provide namespace information, '
                             'completion of NICOS commands and devices is '
                             'disabled')

    def update(self, info):
        """Take over the catalog from *info*, the result of
        ``session.getNamespaceInfo()``; an empty one clears it."""
        self.commands = {c['name']: c for c in info.get('commands', ())}
        self.devices = {d['name']: d for d in info.get('devices', ())}
        self.apis = {name: frozenset(api)
                     for name, api in info.get('apis', {}).items()}
        try:
            writeStubs(self.stubdir, info.get('stubs', {}))
        except OSError as err:
            self.log.warning('could not write the stub modules for the '
                             'editor: %s', err)
        self.available = bool(info)
        self.changed.emit()


def sharedCatalog(client, log):
    """Return the catalog of *client*, created on first use."""
    catalog = getattr(client, 'namespaceCatalog', None)
    if catalog is None:
        catalog = client.namespaceCatalog = Catalog(client, log)
    return catalog


def lineCompleter(client, log):
    """Return the completion callback of a console input line that
    completes members with jedi, see :meth:`CodeAssist.completeLine`;
    everything else is completed by the daemon as without it."""
    assist = CodeAssist(sharedCatalog(client, log))

    def complete(fullstring, lastword):
        try:
            return assist.completeLine(fullstring, lastword)
        except Exception:
            return []
    return complete


class CodeAssist:
    """What the language server does not show, for one front end: the type
    of a value and the description and current values of a device in their
    hover, the members of names that only the daemon knows, and the
    completion of a console line.

    The methods may be called from another thread than that of the catalog;
    tooltips are Markdown with HTML.
    """

    def __init__(self, catalog):
        self.catalog = catalog
        self.client = catalog.client
        self.daemoncomplete = True  # members of unknown names from the daemon
        self.livevalues = True      # show the values of a device in its hover

    def hover(self, text, line, column, before, word):
        """Return what to add to the hover of the language server for
        *word*, at *column* of *line* (1-based) of *text* and after the text
        *before* on its line; None if nothing.

        For a device, its class, description and current values; for other
        values their type, as jedi infers it, e.g. ``bars: list[Bar]``, and
        the docstring of a name that the language server cannot document,
        since jedi infers no value or several for it, e.g. a parameter of a
        device whose type is a union or has no Python equivalent.
        Functions, classes and modules are left to the language server,
        which shows their signature.
        """
        device = None if before.endswith('.') else \
            self.catalog.devices.get(word)
        if device is not None:
            return self._deviceHover(word, device)
        hint = jediTypeHint(self.catalog.stubdir, text, line, column)
        doc = paragraphs(
            jediDocstring(self.catalog.stubdir, text, line, column) or '')
        if not (hint or doc):
            return None
        return '\n\n'.join(
            [fence('%s: %s' % (word, hint) if hint else word)] + doc)

    def _deviceHover(self, name, device):
        header = '%s: %s' % (name, device['class'].rpartition('.')[2])
        if device['alias']:
            header += '  # alias of %s' % device['alias']
        parts = [fence(header)] + paragraphs(device['description'])
        live = self._deviceInfo(name) if self.livevalues else {}
        unit = ' ' + escape(live['unit']) if live.get('unit') else ''
        facts = []
        if 'value' in live:
            facts.append('<b>Value:</b> %s%s' % (escape(live['value']), unit))
            if 'target' in live and live['target'] != live['value']:
                facts.append('<b>Target:</b> %s%s' % (escape(live['target']),
                                                      unit))
        elif device['unit']:
            facts.append('<b>Unit:</b> %s' % escape(device['unit']))
        if live.get('status'):
            facts.append('<b>Status:</b> <span style="%s">%s</span>' %
                         (live['statusstyle'], escape(live['status'])))
        if live.get('limits'):
            facts.append('<b>Limits:</b> %s%s' % (escape(live['limits']),
                                                  unit))
        if facts:
            parts.append('<p>%s</p>' % ' · '.join(facts))
        return '\n\n'.join(parts)

    def members(self, text, line, column):
        """Return the members of devices among the completions at *column*
        of *line* (1-based) of *text*: their names, mapped to whether they
        belong to the user API, the user methods and parameters.  The front
        ends offer the others, methods for drivers and internal ones, only
        once a name is typed."""
        result = {}
        for completion in jediCompletions(self.catalog.stubdir, text, line,
                                          column):
            api = self.catalog.apis.get(
                (completion.full_name or '').rpartition('.')[0])
            if api is not None:
                result[completion.name] = completion.name in api
        return result

    def daemonMembers(self, text, line, column):
        """Return completion items for the members of the dotted name before
        *column* of *line* (1-based) of *text*, from the daemon's completer;
        nothing if the catalog or jedi knows the name.

        Each item is a dict with 'label', 'kind' ('method' or 'field'),
        'insert' (a snippet if 'snippet' is set) and 'detail'.
        """
        if not self.daemoncomplete or not self.client.isconnected:
            return []
        lines = text.split('\n')
        before = lines[line - 1][:column] if 0 < line <= len(lines) else ''
        match = DOTTED_NAME.search(before)
        if not match:
            return []
        name, prefix = match.groups()
        head = name.split('.')[0]
        if head in self.catalog.devices or head in self.catalog.commands or \
           jediCompletions(self.catalog.stubdir, text, line,
                           column - len(prefix)):
            return []
        completions = self.client.ask('complete', before, match.group(),
                                      quiet=True, noerror=True, default=[])
        items = []
        for completion in completions:
            member, paren, _ = \
                str(completion).rsplit('.', 1)[-1].partition('(')
            if member:
                items.append({
                    'label': member, 'detail': 'from the NICOS session',
                    'kind': 'method' if paren else 'field',
                    'insert': member + '($0)' if paren else member,
                    'snippet': bool(paren),
                })
        return items

    def completeLine(self, fullstring, lastword):
        """Return the completions of a console line as the replacements of
        *lastword*, like the daemon's completer: callables end in '('.

        Members, after a dot, are completed by jedi; everything else, and
        what jedi does not know, by the daemon's completer.  As in the
        editor, the members of devices beyond their user API are offered
        only once a name is typed, private members once it begins with "_".
        """
        if match := re.search(r'\.(\w*)$', fullstring):
            prefix = match.group(1)
            stem = lastword.removesuffix(prefix)
            matches = []
            for c in jediCompletions(self.catalog.stubdir, fullstring, 1,
                                     len(fullstring)):
                api = self.catalog.apis.get(
                    (c.full_name or '').rpartition('.')[0])
                if c.type == 'path' or \
                   c.name.startswith('_') and not prefix.startswith('_') or \
                   api is not None and c.name not in api and not prefix:
                    continue
                matches.append(stem + c.name + (
                    '(' if c.type in ('function', 'class') else ''))
            if matches:
                return matches
        return self.client.ask('complete', fullstring, lastword, default=[])

    def _deviceInfo(self, name):
        """Return the current value, target, status and limits of a device
        from the cache, formatted."""
        params = self.client.getDeviceParams(name)
        fmtstr = params.get('fmtstr')
        info = {'unit': params.get('unit') or ''}
        for key in ('value', 'target'):
            if params.get(key) is not None:
                info[key] = _formatValue(params[key], fmtstr)
        status = params.get('status')
        if isinstance(status, (list, tuple)) and len(status) == 2:
            info['status'] = statuses.get(status[0], str(status[0])) + \
                (': %s' % status[1] if status[1] else '')
            info['statusstyle'] = statusStyle(status[0])
        limits = params.get('userlimits')
        if isinstance(limits, (list, tuple)) and len(limits) == 2:
            info['limits'] = '[%s, %s]' % (_formatValue(limits[0], fmtstr),
                                           _formatValue(limits[1], fmtstr))
        return info


def _jediScript(stubdir, text):
    """The script *text* for jedi, which knows the commands and devices of
    the stub modules in *stubdir*."""
    return jedi.Script(HEADER + text, project=jedi.Project(
        stubdir, added_sys_path=[stubdir]))


def jediCompletions(stubdir, text, line, column):
    """Return jedi's completions at *column* of *line* (1-based) of the
    script *text*; nothing without jedi."""
    if jedi is None:
        return []
    with _jediLock:
        try:
            return _jediScript(stubdir, text).complete(line + 1, column)
        except Exception:
            return []


# the generics of the typing module that jedi gives for builtin containers
_TYPING_GENERIC = re.compile(r'\b(List|Dict|Tuple|Set|FrozenSet|Type)\[')


def jediTypeHint(stubdir, text, line, column):
    """Return the type of the value at *column* of *line* (1-based) of the
    script *text* as jedi infers it, e.g. ``list[Bar]``, or ``int | str``
    for alternatives; None for functions, classes and modules, unknown
    values, or without jedi."""
    if jedi is None:
        return None
    hints = []
    with _jediLock:
        try:
            values = _jediScript(stubdir, text).infer(line + 1, column)
        except Exception:
            return None
        for value in values:
            try:
                hint = value.get_type_hint() if value.type == 'instance' \
                    else None
            except Exception:
                hint = None
            if hint:
                hint = _TYPING_GENERIC.sub(
                    lambda match: match.group(1).lower() + '[', hint)
                if hint not in hints:
                    hints.append(hint)
    return ' | '.join(hints) or None


def jediDocstring(stubdir, text, line, column):
    """Return the docstring of the name at *column* of *line* (1-based) of
    the script *text*, if the language server does not document it: if
    jedi infers no value for it, or several of which none has its name, as
    for a parameter of a device whose type is a union or has no Python
    equivalent; None otherwise, or without jedi."""
    if jedi is None:
        return None
    with _jediLock:
        try:
            script = _jediScript(stubdir, text)
            values = script.infer(line + 1, column)
            names = script.goto(line + 1, column)
            if len(values) == 1 or any(value.name == name.name
                                       for value in values for name in names):
                return None
            return next(filter(None, (name.docstring(raw=True)
                                      for name in names)), None)
        except Exception:
            return None


def _formatValue(value, fmtstr):
    if fmtstr and isinstance(value, (int, float)):
        try:
            return fmtstr % value
        except (TypeError, ValueError):
            pass
    return str(value)


def escape(text):
    return html.escape(str(text), quote=False)


def fence(code):
    """Python code in Markdown, highlighted by the hover of Monaco."""
    return '```python\n%s\n```' % code


def paragraphs(text):
    """The paragraphs of *text*, separated by empty lines, in HTML; none for
    an empty text."""
    return ['<p>%s</p>' % escape(p) for p in text.split('\n\n') if p.strip()]


def statusStyle(code):
    """Return the CSS style of a status text in tooltips: the colors of the
    status in the device panel, or empty if the palette is not initialized.

    The hover renderer of Monaco accepts exactly ``color:#...;`` optionally
    followed by ``background-color:#...;``.
    """
    if not hasattr(colors, 'text'):
        return ''
    fg, bg = {
        OK: (colors.dev_fg_ok, None),
        WARN: (colors.text, colors.dev_bg_warning),
        BUSY: (colors.text, colors.dev_bg_busy),
        NOTREACHED: (colors.text, colors.dev_bg_error),
        DISABLED: (colors.text, colors.dev_bg_disabled),
        ERROR: (colors.text, colors.dev_bg_error),
    }.get(code, (colors.dev_fg_unknown, None))
    style = 'color:%s;' % QColor(fg).name()
    if bg is not None:
        style += 'background-color:%s;' % QColor(bg).name()
    return style


# -- stub modules -------------------------------------------------------------

def writeStubs(directory, modules):
    """Write the stub modules from *modules* (file name -> source) into
    *directory*, empty for missing ones; only the known files are written.
    Each file is replaced at once, so that jedi never reads a partial one."""
    for filename in STUB_FILES:
        target = os.path.join(directory, filename)
        with open(target + '.new', 'w', encoding='utf-8') as fp:
            fp.write(modules.get(filename, ''))
        os.replace(target + '.new', target)
