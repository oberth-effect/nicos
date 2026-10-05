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

"""NICOS test lib: Test the Monaco editor widget and editor panel."""

import json
import logging
import signal
import subprocess
import sys

import pytest

from nicos.clients.gui.completion import HEADER, statusStyle
from nicos.clients.gui.widgets.monacoeditor import DEFAULT_SETTINGS, \
    SERVER_RESTARTS, MonacoDocument, MonacoHost, MonacoSettingsDialog, \
    ServerWatchdog, _chainedHandler, _chainSignalHandlers, \
    loadMonacoSettings, saveMonacoSettings
from nicos.core import status
from nicos.guisupport.colors import colors
from nicos.guisupport.qt import QObject, QSettings, QVBoxLayout, QWidget, \
    pyqtSignal

from test.test_gui.test_completion import FakeClient as CodeAssistClient
from test.utils import module_root

try:
    from nicos.clients.gui.panels.monacoeditor import MonacoEditorPanel
except ImportError:  # without qtmonaco
    MonacoEditorPanel = None

pytest.importorskip('pytestqt')


def test_lazy_import():
    """The other panels import the Monaco editor and the code assist only if
    their options select them."""
    modules = ['qtmonaco', 'qtpy', 'nicos.clients.gui.completion',
               'nicos.clients.gui.widgets.monacoeditor',
               'nicos.clients.gui.panels.monacoeditor']
    code = ('import sys\n'
            'import nicos.clients.gui.panels.cmdbuilder\n'
            'import nicos.clients.gui.panels.commandline\n'
            'import nicos.clients.gui.panels.console\n'
            'import nicos.clients.gui.panels.editor\n'
            'print([m for m in %r if m in sys.modules])' % modules)
    result = subprocess.run([sys.executable, '-c', code], cwd=module_root,
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == '[]'


def test_signal_chain():
    """qtmonaco's handlers of SIGINT and SIGTERM call the previous ones."""
    calls = []
    saved = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, lambda *args: calls.append('previous'))
        previous = {signal.SIGINT: signal.getsignal(signal.SIGINT)}
        signal.signal(signal.SIGINT, lambda *args: calls.append('qtmonaco'))
        _chainSignalHandlers(previous)
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        assert calls == ['qtmonaco', 'previous']
        # unchanged handlers are left alone
        handler = signal.getsignal(signal.SIGINT)
        _chainSignalHandlers({signal.SIGINT: handler})
        assert signal.getsignal(signal.SIGINT) is handler
    finally:
        signal.signal(signal.SIGINT, saved)


class FakeProcess:
    returncode = None

    def poll(self):
        return self.returncode


class FakeServer:
    server_process = None
    starts = 0

    def start(self):
        self.starts += 1
        self.server_process = FakeProcess()


def test_server_watchdog(qapp, caplog):
    server = FakeServer()
    watchdog = ServerWatchdog(server, logging.getLogger('test'))
    restarts = []
    watchdog.restarted.connect(lambda: restarts.append(server.starts))
    # not started or stopped on purpose, or running: nothing to do
    watchdog.check()
    server.start()
    watchdog.check()
    assert server.starts == 1
    # ended by itself: started again
    server.server_process.returncode = 1
    watchdog.check()
    assert server.starts == 2 and watchdog.restarts == 1
    assert restarts == [2]
    assert 'has ended (exit code 1), starting it again' in caplog.text
    # after a minute, it counts as the first restart again
    watchdog.lastRestart -= 61
    server.server_process.returncode = 1
    watchdog.check()
    assert server.starts == 3 and watchdog.restarts == 1
    # one that keeps ending right after the start is given up
    assert watchdog.timer.isActive()
    for _ in range(SERVER_RESTARTS):
        server.server_process.returncode = 1
        watchdog.check()
    assert server.starts == 2 + SERVER_RESTARTS
    assert not watchdog.timer.isActive()
    assert 'keeps ending (exit code 1), not starting it again' in caplog.text


def test_settings_storage(tmp_path):
    inifile = str(tmp_path / 'gui.ini')
    changed = dict(DEFAULT_SETTINGS, comment='#', tabsize=2, wordwrap=False)
    settings = QSettings(inifile, QSettings.Format.IniFormat)
    settings.beginGroup('User editor')
    assert loadMonacoSettings(settings) == DEFAULT_SETTINGS
    saveMonacoSettings(settings, changed)
    settings.endGroup()
    settings.sync()
    settings = QSettings(inifile, QSettings.Format.IniFormat)
    settings.beginGroup('User editor')
    assert loadMonacoSettings(settings) == changed


def test_settings_dialog(qtbot):
    dialog = MonacoSettingsDialog(None, DEFAULT_SETTINGS)
    qtbot.addWidget(dialog)
    assert dialog.settings() == DEFAULT_SETTINGS
    dialog.comment.setText('#')
    dialog.tabsize.setValue(2)
    dialog.boxes['daemoncomplete'].setChecked(False)
    assert dialog.settings() == dict(DEFAULT_SETTINGS, comment='#', tabsize=2,
                                     daemoncomplete=False)
    # an empty prefix falls back to the default
    dialog.comment.setText('  ')
    assert dialog.settings()['comment'] == '##'
    dialog.setSettings(DEFAULT_SETTINGS)
    assert dialog.settings() == DEFAULT_SETTINGS


class FakeClient(CodeAssistClient):
    cache = pyqtSignal(object)
    experiment = pyqtSignal(object)
    simresult = pyqtSignal(object)
    simmessage = pyqtSignal(object)

    def __init__(self):
        CodeAssistClient.__init__(self)
        self.runs = []

    def register(self, widget, key):
        pass

    def eval(self, expr, default=None):
        if expr == 'session.experiment.scriptpath':
            return ''
        return CodeAssistClient.eval(self, expr, default)

    def run(self, code, filename=None, noqueue=False):
        self.runs.append((code, filename))


class FakeMainWindow(QObject):
    codeGenerated = pyqtSignal(object)
    instrument = 'test'
    log = logging.getLogger('test')


@pytest.fixture
def host(qtbot, qapp):
    colors.init_palette(qapp.palette())
    container = QWidget()
    layout = QVBoxLayout(container)
    host = MonacoHost(container, FakeClient(), logging.getLogger('test'))
    layout.addWidget(host)
    qtbot.addWidget(container)
    container.resize(600, 400)
    container.show()
    qtbot.waitUntil(lambda: host.bridge_initialized, timeout=30000)
    yield host


@pytest.fixture
def panel(qtbot, tmp_path):
    # keep the panel settings out of the user's configuration
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                      str(tmp_path))
    parent = QWidget()
    parent.mainwindow = FakeMainWindow()
    panel = MonacoEditorPanel(parent, FakeClient(), {})
    QVBoxLayout(parent).addWidget(panel)
    qtbot.addWidget(parent)
    parent.resize(800, 600)
    parent.show()
    qtbot.waitUntil(lambda: panel.monaco.bridge_initialized, timeout=30000)
    yield panel


def page_value(qtbot, host, expression):
    result = []
    host.page().runJavaScript(expression, result.append)
    qtbot.waitUntil(lambda: bool(result), timeout=5000)
    return result[0]


def page_eval(qtbot, host, expression):
    """Evaluate an expression that may yield a promise, return its value."""
    host.page().runJavaScript(
        'window.testValue = undefined; Promise.resolve(%s).then('
        'function (r) { window.testValue = r === undefined ? null '
        ': r; })' % expression)
    qtbot.waitUntil(lambda: page_value(
        qtbot, host, 'window.testValue !== undefined') is True, timeout=5000)
    return page_value(qtbot, host, 'window.testValue')


class Providers:
    """Calls the providers of the page on the text of a document, with the
    cursor at the end of the text or at the given line and column."""

    def __init__(self, qtbot, host, doc):
        self.qtbot = qtbot
        self.host = host
        self.doc = doc

    def _call(self, text, provider, method, then, line=None, column=None):
        self.doc.setText(text)
        lines = text.split('\n')
        if line is None:
            line, column = len(lines), len(lines[-1]) + 1
        return page_eval(self.qtbot, self.host, (
            'Promise.resolve(window.nicosEditor.providers.%s.%s(window.'
            'qtmonaco.editor.getModel(), {lineNumber: %d, column: %d})).then('
            'function (r) { return %s; })') % (provider, method, line, column,
                                               then))

    def complete(self, text, line=None, column=None):
        return self._call(text, 'completion', 'provideCompletionItems',
                          'r.suggestions.map(function (s) { return s.label.'
                          'label || s.label; })', line, column)

    def hover(self, text, line, column):
        return self._call(text, 'hover', 'provideHover',
                          'r ? r.contents[0].value : null', line, column)


@pytest.fixture
def providers(qtbot, host):
    host.assist.catalog.refresh()
    qtbot.waitUntil(lambda: page_value(
        qtbot, host, 'window.nicosEditor.providersInstalled') is True,
        timeout=5000)
    doc = host.createDocument()
    host.showDocument(doc)
    yield Providers(qtbot, host, doc)
    doc.dispose()


@pytest.mark.skipif(MonacoHost is None, reason='qtmonaco is not installed')
class TestMonacoHost:

    def test_documents(self, qtbot, host):
        assert page_value(qtbot, host, 'typeof window.nicosEditor.handle') \
            == 'function'
        doc = host.createDocument()
        host.showDocument(doc)
        changes = []
        doc.modificationChanged.connect(changes.append)
        # the text and the modified state are read from the page
        doc.setText('maw(motor, 1)\n')
        assert doc.text() == 'maw(motor, 1)\n'
        assert doc.isModified()
        doc.setModified(False)
        assert not doc.isModified()

        # edits in the page are seen at once, and the changes of the
        # modified state are reported
        doc.moveToEnd()
        doc.insert('read(motor)\n')
        assert doc.text() == 'maw(motor, 1)\nread(motor)\n'
        assert doc.isModified()
        qtbot.waitUntil(lambda: changes[-1:] == [True], timeout=5000)
        # undoing back to the saved text leaves it unmodified
        doc.undo()
        assert doc.text() == 'maw(motor, 1)\n'
        assert not doc.isModified()
        qtbot.waitUntil(lambda: changes[-1:] == [False], timeout=5000)

        # the byte order mark of a text is kept
        doc.setText('\ufeffx = 1\n')
        assert doc.text() == '\ufeffx = 1\n'

        # a second document is shown without affecting the first
        doc2 = host.createDocument()
        host.showDocument(doc2)
        doc2.setText('x = (')
        assert page_value(qtbot, host, 'window.nicosEditor.models.size') == 2
        assert doc.text() == '\ufeffx = 1\n'

        # lines are separated by LF also after a text without line breaks,
        # which Monaco gives the end of line sequence of the platform
        doc2.moveToEnd()
        doc2.insert('\n1)\n')
        assert doc2.text() == 'x = (\n1)\n'

        doc.dispose()
        doc2.dispose()
        qtbot.waitUntil(lambda: page_value(
            qtbot, host, 'window.nicosEditor.models.size') == 0, timeout=5000)

    def test_signals(self, host):
        # the handlers that qtmonaco installed call the previous ones too
        for signum in (signal.SIGINT, signal.SIGTERM):
            assert getattr(signal.getsignal(signum), 'func', None) is \
                _chainedHandler

    def test_providers(self, qtbot, host, providers):
        """The providers of the page forward to the code assist of the host
        and convert its answers; the logic itself is tested in
        test_completion, the stubs for the language server there too."""
        assert host.get_lsp_header() == HEADER
        suggest = page_value(qtbot, host, 'window.qtmonaco.editor.getOption('
                             'window.qtmonaco.monaco.editor.EditorOption.'
                             'suggest)')
        assert suggest['showSnippets'] is False
        assert suggest['filterGraceful'] is False
        # members of names that only the daemon knows
        complete = providers.complete
        assert complete('s.') == ['target']
        assert complete('x = s.ta') == ['target']
        # everything else is left to the language server
        assert complete('motor.') == []
        assert complete('ma') == []
        assert complete("'s.") == []
        assert complete('# s.') == []
        # the hover of a device shows its values with the colored status
        device = providers.hover('maw(motor, 1)', 1, 7)
        assert device.startswith('```python\nmotor: VirtualMotor\n```\n\n'
                                 '<p>a motor</p>')
        assert '<b>Status:</b> <span style="%s">ok: idle</span>' % \
            statusStyle(status.OK) in device
        assert providers.hover('maw(motor, 1)', 1, 2) is None
        assert providers.hover('unknown.motor', 1, 10) is None
        # the type of a variable, which the language server does not show
        assert providers.hover('lst = motor.getBars()\nlst', 2, 2) == \
            '```python\nlst: list[Bar]\n```'

    def test_items(self, qtbot, host, providers):
        """The completion items of the language server get the kinds of
        Monaco, and the NICOS commands and devices a description."""
        # completion providers registered by others, like qtmonaco for the
        # language server, are wrapped
        assert page_value(qtbot, host, (
            '(function () { var n = window.nicosEditor.wrappedProviders; '
            'window.qtmonaco.monaco.languages.registerCompletionItemProvider('
            "'plaintext', {provideCompletionItems: function () {}}).dispose();"
            ' return window.nicosEditor.wrappedProviders - n; })()')) == 1
        names = page_value(qtbot, host, 'window.nicosEditor.names')
        assert names['maw'] == {'description': 'NICOS command',
                                'detail': 'Move.'}
        assert names['motor'] == {'description': 'VirtualMotor',
                                  'detail': 'a motor'}

        def adapt(text, items, members=None, incomplete=False):
            providers.doc.setText(text)
            result = page_value(qtbot, host, (
                '(function () { var m = window.qtmonaco.editor.getModel(); '
                'var l = m.getLineCount(); return window.nicosEditor.'
                'adaptItems(m, {lineNumber: l, column: m.getLineMaxColumn(l)}'
                ', {suggestions: %s}, %s); })()') % (
                    json.dumps(items), json.dumps(members)))
            assert result.get('incomplete', False) == incomplete
            return result['suggestions']

        # functions and methods are inserted as a call, with the cursor
        # between the brackets and the parameter hints open
        assert page_value(qtbot, host, 'window.qtmonaco.monaco.languages.'
                          'CompletionItemInsertTextRule.InsertAsSnippet') == 4

        def call(name):
            return {'insertText': name + '($0)', 'insertTextRules': 4,
                    'command': {'id': 'editor.action.triggerParameterHints',
                                'title': 'Parameter hints'}}

        snippet = {'label': 'for loop', 'kind': 27, 'range': {
            'startLineNumber': 0, 'startColumn': 0, 'endLineNumber': 0,
            'endColumn': 0}}
        items = adapt('m', [{'label': 'maw(dev, pos, args)', 'kind': 3},
                            {'label': 'motor', 'kind': 6},
                            {'label': 'map', 'kind': 7}, snippet])
        assert items[0] == {'label': {'label': 'maw(dev, pos, args)',
                                      'description': 'NICOS command'},
                            'kind': 1, 'detail': 'Move.',
                            **call('maw')}  # a function
        assert items[1] == {'label': {'label': 'motor',
                                      'description': 'VirtualMotor'},
                            'kind': 4, 'detail': 'a motor'}  # a variable
        assert items[2] == {'label': 'map', 'kind': 5}  # a class
        # the snippets of qtmonaco are left alone
        assert items[3] == snippet
        # members are no commands or devices
        assert adapt('x.m', [{'label': 'motor', 'kind': 10}]) == \
            [{'label': 'motor', 'kind': 9}]  # a property
        # of the members of devices, the user API is marked, the others are
        # offered once a name is typed
        members = [{'label': 'doRead(maxage)', 'kind': 2},
                   {'label': 'read(maxage)', 'kind': 2},
                   {'label': 'speed', 'kind': 10},
                   {'label': 'format(value)', 'kind': 2},
                   {'label': '__class__', 'kind': 10}]
        api = {'doRead': False, 'read': True, 'speed': True, 'format': False}
        user = [{'label': {'label': 'read(maxage)',
                           'description': 'user method'}, 'kind': 0,
                 **call('read')},
                {'label': {'label': 'speed', 'description': 'parameter'},
                 'kind': 9}]
        assert adapt('motor.', members, api, incomplete=True) == user
        assert adapt('motor.f', members, api) == [
            {'label': 'doRead(maxage)', 'kind': 0, **call('doRead')}, *user,
            {'label': 'format(value)', 'kind': 0, **call('format')}]
        # private members once the name begins with "_", of any value
        assert {'label': '__class__', 'kind': 9} in \
            adapt('motor._', members, api)
        assert adapt('x.', [{'label': '__class__', 'kind': 10}],
                     incomplete=True) == []

    def test_settings(self, qtbot, host, providers):
        doc = providers.doc

        def markers():
            return page_value(qtbot, host, 'window.qtmonaco.monaco.editor.'
                              "getModelMarkers({owner: 'nicos'}).length")

        # the syntax check marks the error
        doc.setText('x = (')
        qtbot.waitUntil(lambda: markers() == 1, timeout=5000)

        host.applySettings({'syntaxcheck': False, 'daemoncomplete': False,
                            'livevalues': False, 'comment': '#',
                            'wordwrap': False, 'tabsize': 2, 'typos': True})
        qtbot.waitUntil(lambda: markers() == 0, timeout=5000)
        assert page_value(qtbot, host, 'window.nicosEditor.comment') == '#'
        assert page_value(qtbot, host, 'window.qtmonaco.editor.getOption('
                          'window.qtmonaco.monaco.editor.EditorOption.'
                          'wordWrap)') == 'off'
        assert page_value(qtbot, host, 'window.qtmonaco.editor.getModel().'
                          'getOptions().tabSize') == 2
        assert page_value(qtbot, host, 'window.qtmonaco.editor.getOption('
                          'window.qtmonaco.monaco.editor.EditorOption.'
                          'suggest).filterGraceful') is True
        # the daemon is not asked about unknown names, and device tooltips
        # come from the catalog only
        assert providers.complete('s.') == []
        hover = providers.hover('motor', 1, 2)
        assert hover.startswith('```python\nmotor: VirtualMotor\n```')
        assert '<b>Value:</b>' not in hover

        # back to the defaults
        host.applySettings({})
        doc.setText('x = (')
        qtbot.waitUntil(lambda: markers() == 1, timeout=5000)
        assert providers.complete('s.') == ['target']

    def test_server_restart(self, qtbot, host):
        """A language server that has ended, e.g. from the bug of pylsp, is
        started again, and the page connects to it, also if it ended before
        the connection was initialized."""
        from qtmonaco.pylsp_provider import pylsp_server

        def wrapped():
            return page_value(qtbot, host,
                              'window.nicosEditor.wrappedProviders')

        # the completion provider of a connection is wrapped, as soon as
        # the connection is opened
        qtbot.waitUntil(lambda: wrapped() > 0, timeout=20000)
        before, port = wrapped(), pylsp_server.port
        pylsp_server.server_process.kill()
        pylsp_server.server_process.wait()
        ServerWatchdog.instance.check()
        assert pylsp_server.is_running() and pylsp_server.port == port
        qtbot.waitUntil(lambda: wrapped() > before, timeout=30000)


@pytest.mark.skipif(MonacoHost is None, reason='qtmonaco is not installed')
class TestEditorPanel:

    def test_panel(self, qtbot, panel, tmp_path):
        client = panel.client
        host = panel.monaco
        assert panel.monaco.settings == DEFAULT_SETTINGS
        # the view of the documents is laid out below the tab bar
        assert panel.mainFrame.layout().indexOf(host) == 1
        assert host.isVisible() and host.height() > 0
        assert host.y() >= panel.tabber.geometry().bottom()
        assert panel.actionEditorSettings in panel.getMenus()[1].actions()
        assert panel.actionEditorSettings in panel.getToolbars()[0].actions()
        assert len(panel.editors) == 1
        editor = panel.currentEditor
        assert isinstance(editor, MonacoDocument)

        # changes mark the document dirty
        editor.setText('maw(motor, 1)\n')
        qtbot.waitUntil(lambda: panel.tabber.tabText(0).endswith('*'),
                        timeout=5000)
        assert panel.actionSave.isEnabled()
        editor.setModified(False)
        qtbot.waitUntil(lambda: not panel.tabber.tabText(0).endswith('*'),
                        timeout=5000)

        # running sends the current text of the page
        panel.current_status = 'idle'
        panel.on_actionRun_triggered()
        assert client.runs == [('maw(motor, 1)\n', '')]

        # the actions that differ from the base class run once
        calls = []
        host.trigger = calls.append
        panel.actionFind.trigger()
        panel.actionComment.trigger()
        assert calls == ['editor.action.startFindReplaceAction',
                         'editor.action.commentLine']

        # opening files replaces the untouched new file, the text file is
        # not treated as Python
        script = tmp_path / 'script.py'
        script.write_text('read(motor)\n')
        spm = tmp_path / 'script.txt'
        spm.write_text('read motor\n')
        panel.openFile(str(script))
        panel.openFile(str(spm))
        assert [panel.filenames[e] for e in panel.editors] == \
            [str(script), str(spm)]
        assert panel.currentEditor.text() == 'read motor\n'
        qtbot.waitUntil(lambda: page_value(
            qtbot, host, 'window.nicosEditor.models.size') == 2, timeout=5000)
        assert page_value(qtbot, host, 'window.nicosEditor.models.get(%d).'
                          'model.getLanguageId()' % panel.currentEditor.docid
                          ) == 'plaintext'

        # switching tabs shows the other document
        panel.tabber.setCurrentIndex(0)
        assert panel.currentEditor.text() == 'read(motor)\n'
        assert page_value(qtbot, host, 'window.nicosEditor.currentId') == \
            panel.currentEditor.docid

        # saving writes the current text of the page
        panel.currentEditor.setText('read(motor)\nread(axis)\n')
        panel.on_actionSave_triggered()
        assert script.read_text() == 'read(motor)\nread(axis)\n'
        assert not panel.currentEditor.isModified()
        # an edit is seen at once, e.g. when the panel is closed
        panel.currentEditor.insert('x = 1\n')
        assert panel.currentEditor.isModified()

        # closing a tab removes its document from the page
        panel.tabber.setCurrentIndex(1)
        panel._close(panel.currentEditor)
        assert len(panel.editors) == 1
        qtbot.waitUntil(lambda: page_value(
            qtbot, host, 'window.nicosEditor.models.size') == 1, timeout=5000)
