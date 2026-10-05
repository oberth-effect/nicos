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

"""Monaco based script editor widget for the NICOS GUI.

The editor panel shows all its scripts in one :class:`MonacoHost`, a web
view running the Monaco editor from the ``qtmonaco`` package.  Each script
is a :class:`MonacoDocument`: a text model in that view, with a widget that
serves as the tab page.

Completion, hover and signature help come from the language server of
qtmonaco (jedi), which knows the commands and devices of the connected
daemon from the stub modules of the catalog: the host puts their directory
on the path of jedi and prepends the header importing them to every
document (see :mod:`nicos.clients.gui.completion`).

The companion ``monacoeditor.js`` is injected into the page.  It manages the
models, labels the suggestions of the language server (commands and the user
API of devices) and adds those for members that only the daemon knows, and
registers a hover provider for the types of values and the current values
of devices.  Its requests are answered by the :class:`CodeAssist` of the
host, in a thread, since they may ask the daemon.  The page talks to the
host through the ``nicos`` object (:class:`MonacoBridge`) on the web channel
of the view; the host sends commands to the page with ``runJavaScript``.
"""

import functools
import json
import signal
import threading
import time
from os import path

from nicos.clients.gui.completion import HEADER, CodeAssist, sharedCatalog
from nicos.guisupport.qt import QApplication, QCheckBox, QDialog, \
    QDialogButtonBox, QEventLoop, QFontInfo, QFormLayout, QGroupBox, \
    QLineEdit, QObject, QSpinBox, QTextDocument, QTimer, QVBoxLayout, \
    QWebEnginePage, QWebEngineScript, QWidget, pyqtSignal, pyqtSlot

try:
    from qtmonaco import Monaco
except Exception as err:  # also e.g. ValueError from qtpy for an unknown API
    Monaco = None
    MONACO_ERROR = err
else:
    MONACO_ERROR = None

JS_FILE = path.join(path.dirname(__file__), 'monacoeditor.js')

CALL_TIMEOUT = 2000  # ms to wait for the page to answer a query

# request kind of the page -> method of the CodeAssist answering it
REQUESTS = {'complete': CodeAssist.daemonMembers,
            'members': CodeAssist.members, 'hover': CodeAssist.hover}

# the language server is checked this often (ms); it is given up after this
# many restarts, each within a minute of the previous one
SERVER_CHECK_INTERVAL = 2000
SERVER_RESTARTS = 3

# Settings the user can change in the settings dialog of the editor panel;
# they are stored in the "monaco" subgroup of the panel settings.
DEFAULT_SETTINGS = {
    'comment': '##',         # prefix used to comment lines out
    'tabsize': 4,
    'wordwrap': True,
    'minimap': False,
    'syntaxcheck': True,     # mark syntax errors
    'quicksuggest': True,    # show suggestions while typing
    'daemoncomplete': True,  # ask the daemon about members of unknown names
    'livevalues': True,      # current device values in tooltips
    'wordsuggest': False,    # suggest words of the document
    'snippets': False,       # suggest the code snippets of qtmonaco
    'typos': False,          # match suggestions despite small typos
}


def loadMonacoSettings(qsettings):
    """Read the editor settings from the ``monaco`` subgroup of
    *qsettings*, with defaults for missing ones."""
    return {key: qsettings.value('monaco/' + key, default, type(default))
            for key, default in DEFAULT_SETTINGS.items()}


def saveMonacoSettings(qsettings, settings):
    """Write the editor settings to the ``monaco`` subgroup of *qsettings*."""
    for key, default in DEFAULT_SETTINGS.items():
        qsettings.setValue('monaco/' + key, settings.get(key, default))


class MonacoSettingsDialog(QDialog):
    """Dialog for the settings of the Monaco based editor, see
    :data:`DEFAULT_SETTINGS`."""

    EDITING = [
        # (settings key, label, tooltip)
        ('wordwrap', 'Wrap long lines', ''),
        ('minimap', 'Show the minimap', ''),
        ('syntaxcheck', 'Mark syntax errors',
         'Checks the Python syntax while typing'),
    ]
    SUGGESTIONS = [
        ('quicksuggest', 'Show suggestions while typing',
         'Otherwise suggestions are shown after a dot and with Ctrl+Space'),
        ('daemoncomplete', 'Ask the daemon about members of unknown names',
         'Uses the completer of the daemon for names the editor does not '
         'know, e.g. variables of a script that has run.  It reads the '
         'attributes of the object; for a device this includes its volatile '
         'parameters.'),
        ('livevalues', 'Show current device values in tooltips',
         'Value, target, status and limits from the cache'),
        ('wordsuggest', 'Suggest words of the document', ''),
        ('snippets', 'Suggest code snippets', ''),
        ('typos', 'Match suggestions despite small typos',
         'Otherwise a suggestion has to contain the typed characters in '
         'this order'),
    ]

    def __init__(self, parent, settings):
        QDialog.__init__(self, parent)
        self.setWindowTitle('Editor settings')
        self.boxes = {}
        self.comment = QLineEdit(self)
        self.comment.setToolTip('Prefix inserted by the "Comment" action; '
                                'a space is added after it')
        self.tabsize = QSpinBox(self)
        self.tabsize.setRange(1, 8)

        layout = QVBoxLayout(self)
        editing = QGroupBox('Editing', self)
        form = QFormLayout(editing)
        form.addRow('Comment prefix:', self.comment)
        form.addRow('Indentation width:', self.tabsize)
        for key, label, tooltip in self.EDITING:
            form.addRow(self._checkbox(key, label, tooltip))
        layout.addWidget(editing)
        suggestions = QGroupBox('Suggestions and tooltips', self)
        vbox = QVBoxLayout(suggestions)
        for key, label, tooltip in self.SUGGESTIONS:
            vbox.addWidget(self._checkbox(key, label, tooltip))
        layout.addWidget(suggestions)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel |
            QDialogButtonBox.StandardButton.RestoreDefaults, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults)\
            .clicked.connect(lambda *args: self.setSettings(DEFAULT_SETTINGS))
        layout.addWidget(buttons)
        self.setSettings(settings)

    def _checkbox(self, key, label, tooltip):
        box = QCheckBox(label, self)
        box.setToolTip(tooltip)
        self.boxes[key] = box
        return box

    def setSettings(self, settings):
        self.comment.setText(settings['comment'])
        self.tabsize.setValue(settings['tabsize'])
        for key, box in self.boxes.items():
            box.setChecked(settings[key])

    def settings(self):
        result = {
            'comment': self.comment.text().strip() or
            DEFAULT_SETTINGS['comment'],
            'tabsize': self.tabsize.value(),
        }
        result.update((key, box.isChecked())
                      for key, box in self.boxes.items())
        return result


def editorOptions(settings):
    """Return the Monaco editor options for the editor *settings*."""
    return {
        'tabSize': settings['tabsize'],
        'insertSpaces': True,
        'detectIndentation': False,
        'wordWrap': 'on' if settings['wordwrap'] else 'off',
        'minimap': {'enabled': settings['minimap']},
        'scrollBeyondLastLine': False,
        'quickSuggestions': {'other': settings['quicksuggest'],
                             'comments': False, 'strings': False},
        # by default, suggest only what the language server and the daemon
        # know: not arbitrary words of the document, and not the snippets of
        # qtmonaco, which it offers regardless of context
        'wordBasedSuggestions': 'matchingDocuments' if settings['wordsuggest']
        else 'off',
        # with the typo tolerance of Monaco, typing "Foo(" keeps "format(...)"
        # of the language server as the only suggestion
        'suggest': {'showSnippets': settings['snippets'],
                    'filterGraceful': settings['typos']},
        'fixedOverflowWidgets': True,
        'unicodeHighlight': {'ambiguousCharacters': False},
    }


def pylspSettings(stubdir):
    """Return the configuration of the language server, with the stub
    modules of the catalog in *stubdir* on the path of jedi.

    The language server is used for completion, hover and signature help
    only; syntax errors are shown by the editor itself.
    """
    plugins = {name: {'enabled': False} for name in (
        'pyflakes', 'pycodestyle', 'mccabe', 'pydocstyle', 'flake8',
        'pylint', 'rope_autoimport')}
    plugins['jedi'] = {'extra_paths': [stubdir],
                       'prioritize_extra_paths': True}
    plugins['jedi_completion'] = {'enabled': True, 'include_params': True,
                                  'fuzzy': False}
    plugins['jedi_hover'] = {'enabled': True}
    plugins['jedi_signature_help'] = {'enabled': True}
    return {'pylsp': {'plugins': plugins}}


class MonacoDocument(QWidget):
    """One script in a :class:`MonacoHost`.

    The widget is only the placeholder page in the tab widget of the panel;
    the text lives in a model of the host.  The methods are those that the
    panel calls on the QScintilla widget, plus a few for Monaco documents
    only.  :meth:`text` and :meth:`isModified` ask the page and wait for the
    answer, so that they are never behind the last keystroke; the page
    reports changes of the modified state as they happen, and the text for
    the syntax check after a pause in typing.
    """

    modificationChanged = pyqtSignal(bool)

    def __init__(self, host, docid):
        QWidget.__init__(self, host.parent())
        self.setFixedHeight(0)
        self.host = host
        self.docid = docid
        self._text = ''         # the last known text, if the page is not ready
        self._modified = False  # as last reported by the page
        self._python = True

    # -- methods of the QScintilla widget used by the panel

    def text(self):
        text = self.host.call('window.nicosEditor.text(%d)' % self.docid)
        if isinstance(text, str):
            self._text = text
        return self._text

    def setText(self, text):
        self._text = text
        self.host.command('setText', model=self.docid, text=text)

    def clear(self):
        self.setText('')

    def insert(self, text):
        self.host.command('insert', model=self.docid, text=text)

    def moveToEnd(self):
        self.host.command('moveToEnd', model=self.docid)

    def beginUndoAction(self):
        self.host.command('undoStop', model=self.docid)

    endUndoAction = beginUndoAction

    def setModified(self, modified):
        self.host.command('setModified', model=self.docid, modified=modified)

    def isModified(self):
        modified = self.host.call('window.nicosEditor.isModified(%d)' %
                                  self.docid)
        return modified if isinstance(modified, bool) else self._modified

    def setFocus(self, *args):
        self.host.setFocus()
        self.host.command('focus')

    def undo(self):
        self.host.trigger('undo')

    def redo(self):
        self.host.trigger('redo')

    def cut(self):
        self.host.page().triggerAction(QWebEnginePage.WebAction.Cut)

    def copy(self):
        self.host.page().triggerAction(QWebEnginePage.WebAction.Copy)

    def paste(self):
        self.host.page().triggerAction(QWebEnginePage.WebAction.Paste)

    def print(self, printer):  # pylint: disable=redefined-builtin
        document = QTextDocument()
        document.setDefaultFont(self.host.font())
        document.setPlainText(self.text())
        document.print(printer)

    # -- methods used by the panel for Monaco documents only

    def setFileName(self, filename):
        """Adapt the language and the syntax check to the file type."""
        python = not filename.endswith('.txt')
        if python != self._python:
            self._python = python
            self.host.command('setLanguage', model=self.docid,
                              language='python' if python else 'plaintext')
            self.updateLint()

    def dispose(self):
        """Remove the document from the page, and delete the widget."""
        self.host.disposeDocument(self.docid)
        self.deleteLater()

    # -- notifications of the page, through the host

    def pageModified(self, modified):
        if modified != self._modified:
            self._modified = modified
            self.modificationChanged.emit(modified)

    def pageChanged(self, text):
        self._text = text
        if self._python and self.host.settings['syntaxcheck']:
            self._lint(text)

    def updateLint(self):
        """Mark the syntax errors of the text, or clear the markers."""
        if self._python and self.host.settings['syntaxcheck']:
            self._lint(self.text())
        else:
            self.host.command('markers', model=self.docid, markers=[])

    def _lint(self, text):
        markers = []
        try:
            compile(text, '<script>', 'exec')
        except SyntaxError as err:
            markers.append({
                'line': err.lineno, 'column': err.offset,
                'endLine': getattr(err, 'end_lineno', None),
                'endColumn': getattr(err, 'end_offset', None),
                'message': '%s: %s' % (type(err).__name__, err.msg)})
        except ValueError as err:
            markers.append({'message': str(err)})
        self.host.command('markers', model=self.docid, markers=markers)


def _chainSignalHandlers(previous):
    """Make new handlers of the signals in *previous* (signal -> handler
    before) call the previous handler after themselves.

    When qtmonaco starts its language server, it replaces the SIGINT and
    SIGTERM handlers of the process with one that only stops the server, so
    that these signals would no longer end the GUI.
    """
    for signum, before in previous.items():
        handler = signal.getsignal(signum)
        if handler is not before and callable(handler):
            signal.signal(signum, functools.partial(_chainedHandler, handler,
                                                    before))


def _chainedHandler(handler, previous, signum, frame):
    handler(signum, frame)
    if callable(previous):
        previous(signum, frame)
    elif previous != signal.SIG_IGN:
        # the default action (also for a handler not set from Python), e.g.
        # ending the process
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)


class ServerWatchdog(QObject):
    """Starts the language server of qtmonaco (*server*) again when it has
    ended by itself.

    pylsp ends its WebSocket server when a client disconnects while an
    answer is sent to it, e.g. when a window with an editor is closed during
    a request.  The server is started again on the same port, and the views
    connect their pages to it anew on :attr:`restarted`: the client of a
    page reconnects by itself only if the server ended after the connection
    was initialized.  A server that keeps ending right after the start is
    given up.
    """

    instance = None  # the one of the process, since the views share a server

    restarted = pyqtSignal()

    def __init__(self, server, log, parent=None):
        QObject.__init__(self, parent)
        self.server = server
        self.log = log
        self.restarts = 0
        self.lastRestart = None  # its time
        self.timer = QTimer(self, interval=SERVER_CHECK_INTERVAL)
        self.timer.timeout.connect(self.check)
        self.timer.start()

    @classmethod
    def watch(cls, log):
        """Watch the server of qtmonaco, once it has been started."""
        if cls.instance is None:
            # imported only now, since the import replaces signal handlers
            from qtmonaco.pylsp_provider import pylsp_server
            cls.instance = cls(pylsp_server, log, QApplication.instance())
        return cls.instance

    def check(self):
        process = self.server.server_process
        # no process once the server is stopped, at exit or on a signal
        if process is None or process.poll() is None:
            return
        now = time.monotonic()
        if self.lastRestart is None or now - self.lastRestart > 60:
            self.restarts = 0
        if self.restarts >= SERVER_RESTARTS:
            self.log.warning('the language server of the editor keeps '
                             'ending (exit code %s), not starting it again',
                             process.returncode)
            self.timer.stop()
            return
        self.log.warning('the language server of the editor has ended (exit '
                         'code %s), starting it again', process.returncode)
        self.restarts += 1
        self.lastRestart = now
        self.server.start()
        self.restarted.emit()


class MonacoBridge(QObject):
    """The object through which the page talks to its host, ``nicos`` on the
    web channel of the view.  The arguments are JSON texts."""

    # the answer to a request of the page: its id and the result
    answered = pyqtSignal(int, str)

    def __init__(self, host):
        QObject.__init__(self, host)
        self._host = host

    @pyqtSlot(str)
    def notify(self, event):
        """An event of the page: changes of documents."""
        self._host.pageEvent(json.loads(event))

    @pyqtSlot(int, str, str)
    def request(self, reqid, kind, payload):
        """A request of the page, answered later by :attr:`answered`."""
        self._host.answerRequest(reqid, kind, json.loads(payload))


if Monaco is not None:

    class MonacoHost(Monaco):
        """The Monaco web view of an editor panel, showing one of its
        documents at a time.

        The host configures the language server for the catalog of the
        client and answers the hover and completion requests of the page
        from its :class:`CodeAssist`.
        """

        # request id, result and error of a request of the page, from the
        # thread that answered it
        requestAnswered = pyqtSignal(object, object, object)

        def __init__(self, parent, client, log):
            previous = {signum: signal.getsignal(signum)
                        for signum in (signal.SIGINT, signal.SIGTERM)}
            Monaco.__init__(self, parent)
            _chainSignalHandlers(previous)
            ServerWatchdog.watch(log).restarted.connect(self._reconnect)
            self.log = log
            self.settings = dict(DEFAULT_SETTINGS)
            self.documents = {}
            self._nextid = 1
            self._pending = []  # commands sent before the page is ready
            self.assist = CodeAssist(sharedCatalog(client, log))
            # the page takes both only before it connects to the server
            self.set_lsp_header(HEADER)
            self.update_workspace_configuration(
                pylspSettings(self.assist.catalog.stubdir))
            # registered before the page has loaded, which gets the objects
            # of the channel only once
            self.bridge = MonacoBridge(self)
            self.page().webChannel().registerObject('nicos', self.bridge)
            self.requestAnswered.connect(self._sendAnswer)
            self.assist.catalog.changed.connect(self._sendNames)
            self.initialized.connect(self._onInitialized)

        # -- qtmonaco hooks

        def _load_editor(self):
            # the page script has to be injected before the page loads
            script = QWebEngineScript()
            script.setName('nicos')
            with open(JS_FILE, encoding='utf-8') as fp:
                script.setSourceCode(fp.read())
            script.setInjectionPoint(
                QWebEngineScript.InjectionPoint.DocumentReady)
            script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
            self.page().scripts().insert(script)
            Monaco._load_editor(self)

        def _reconnect(self):
            # the language server was started again: qtmonaco's _set_host()
            # replaces the client of the page with a new one, with the
            # header and the configuration of the workspace
            if self.bridge_initialized:
                self._set_host(self.pylsp_host)

        def _onInitialized(self):
            pending, self._pending = self._pending, []
            self._sendSettings()
            for code in pending:
                self.page().runJavaScript(code)
            self.command('providers')
            self._sendNames()

        def _sendNames(self):
            # the description, shown next to the label, and the detail of the
            # completion items of the commands and devices
            catalog = self.assist.catalog
            names = {name: {'description': 'NICOS command',
                            'detail': cmd['summary']}
                     for name, cmd in catalog.commands.items()}
            names.update((name, {'description':
                                 dev['class'].rpartition('.')[2],
                                 'detail': dev['description']})
                         for name, dev in catalog.devices.items())
            self.command('names', names=names)

        # -- communication with the page

        def pageEvent(self, event):
            """Handle an event of the page, see :class:`MonacoBridge`."""
            document = self.documents.get(event['model'])
            if document is None:  # disposed meanwhile
                return
            if event['type'] == 'modified':
                document.pageModified(event['modified'])
            elif event['type'] == 'changed':
                document.pageChanged(event['text'])

        def command(self, cmd, **payload):
            """Send a command to the NICOS script in the page."""
            payload['cmd'] = cmd
            code = 'window.nicosEditor.handle(%s)' % \
                json.dumps(payload, default=str)
            if self.bridge_initialized:
                self.page().runJavaScript(code)
            else:
                self._pending.append(code)

        def trigger(self, handler):
            """Run a command or action of the editor on the shown document."""
            self.command('trigger', handler=handler)

        def call(self, expression):
            """Evaluate *expression*, which gives a JSON text, in the page
            and return its value; None if the page is not ready or does not
            answer in time.

            The answer is awaited in a local event loop without user input,
            so that nothing the user does can come in between.  The page
            runs the commands sent before first.
            """
            if not self.bridge_initialized:
                return None
            result = []
            loop = QEventLoop()

            def done(value):
                result.append(value)
                loop.quit()
            self.page().runJavaScript(expression, done)
            if not result:
                QTimer.singleShot(CALL_TIMEOUT, loop.quit)
                loop.exec(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            try:
                return json.loads(result[0]) if result else None
            except (TypeError, ValueError):
                return None

        def answerRequest(self, reqid, kind, payload):
            """Answer a hover or completion request of the page in a thread,
            since the answer may need the daemon or jedi."""
            threading.Thread(target=self._answer, daemon=True,
                             args=(reqid, kind, payload)).start()

        def _answer(self, reqid, kind, payload):
            result = error = None
            try:
                result = REQUESTS[kind](self.assist, **payload)
            except Exception as err:
                error = '%s request from the editor page failed: %s' % (
                    kind, err)
            try:
                self.requestAnswered.emit(reqid, result, error)
            except RuntimeError:  # the host has been deleted meanwhile
                pass

        def _sendAnswer(self, reqid, result, error):
            if error:
                self.log.warning(error)
            self.bridge.answered.emit(reqid, json.dumps(result, default=str))

        # -- documents

        def createDocument(self):
            document = MonacoDocument(self, self._nextid)
            self._nextid += 1
            self.documents[document.docid] = document
            self.command('createModel', model=document.docid)
            return document

        def showDocument(self, document):
            self.command('showModel',
                         model=document.docid if document else None)

        def disposeDocument(self, docid):
            if self.documents.pop(docid, None) is not None:
                self.command('disposeModel', model=docid)

        # -- settings and style

        def applySettings(self, settings):
            """Apply the editor settings of the user, see DEFAULT_SETTINGS."""
            before = self.settings
            self.settings = dict(DEFAULT_SETTINGS, **settings)
            self.assist.daemoncomplete = self.settings['daemoncomplete']
            self.assist.livevalues = self.settings['livevalues']
            if self.bridge_initialized:
                self._sendSettings()
            if self.settings['syntaxcheck'] != before['syntaxcheck']:
                for document in self.documents.values():
                    document.updateLint()

        def _sendSettings(self):
            self.command('options', options=editorOptions(self.settings))
            self.command('settings', comment=self.settings['comment'])

        def setStyle(self, font, back):
            """Apply the user font and background color of the panel."""
            self.setFont(font)
            info = QFontInfo(font)
            self.command('options', options={
                'fontFamily': info.family(),
                'fontSize': max(8, info.pixelSize()),
            })
            if back is not None and back.isValid():
                self.page().setBackgroundColor(back)
                self.command('theme', background=back.name(),
                             base='vs' if back.lightness() >= 128
                             else 'vs-dark')

else:
    MonacoHost = None
