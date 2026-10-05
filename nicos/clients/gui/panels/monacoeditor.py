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

"""NICOS GUI script editor panel with the Monaco editor."""

from nicos.clients.gui.panels.editor import EditorPanel
from nicos.clients.gui.widgets.monacoeditor import MONACO_ERROR, MonacoHost, \
    MonacoSettingsDialog, loadMonacoSettings, saveMonacoSettings
from nicos.guisupport.qt import QAction, QBoxLayout, QDialog, QIcon, \
    QPrintDialog, QPrinter, QSizePolicy, pyqtSlot

if MonacoHost is None:
    raise ImportError('the Monaco editor is not available: %s' % MONACO_ERROR)


class MonacoEditorPanel(EditorPanel):
    """The script editor panel with the Monaco editor instead of QScintilla.

    It adds completion, hover help and signature help for the commands and
    devices of the connected daemon (see
    :mod:`nicos.clients.gui.completion`), and needs the ``qtmonaco``
    package and Qt 6.  The options are those of :class:`EditorPanel`.
    """

    def __init__(self, parent, client, options):
        EditorPanel.__init__(self, parent, client, options)
        # the tab widget shows only its tab bar, the view below it shows the
        # document of the current tab
        layout = self.mainFrame.layout()
        layout.setDirection(QBoxLayout.Direction.TopToBottom)
        layout.addWidget(self.monaco, 1)  # created by loadSettings()
        self.tabber.setSizePolicy(QSizePolicy.Policy.Preferred,
                                  QSizePolicy.Policy.Maximum)
        self.actionEditorSettings = QAction(
            QIcon(':/settings_applications'), 'Editor settings...', self)
        self.actionEditorSettings.triggered.connect(self.editSettings)

    def loadSettings(self, settings):
        EditorPanel.loadSettings(self, settings)
        # the view shared by all documents, before the first one is created
        self.monaco = MonacoHost(self, self.client, self.log)
        self.monaco.applySettings(loadMonacoSettings(settings))

    def createEditor(self):
        editor = self.monaco.createDocument()
        editor.modificationChanged.connect(
            lambda dirty: self.setDirty(editor, dirty))
        self._updateStyle(editor)
        return editor

    def getMenus(self):
        menus = EditorPanel.getMenus(self)
        menus[1].addAction(self.actionEditorSettings)  # the View menu
        return menus

    def getToolbars(self):
        created = self.bar is None
        bars = EditorPanel.getToolbars(self)
        if created:
            bars[0].addSeparator()
            bars[0].addAction(self.actionEditorSettings)
        return bars

    def _updateStyle(self, editor):
        if self.custom_font is not None:
            self.monaco.setStyle(self.custom_font, self.custom_back)

    def enableFileActions(self, on):
        EditorPanel.enableFileActions(self, on)
        self.actionComment.setEnabled(on)

    def on_tabber_currentChanged(self, index):
        EditorPanel.on_tabber_currentChanged(self, index)
        self.monaco.showDocument(self.currentEditor)

    def _close(self, editor):
        EditorPanel._close(self, editor)
        if editor not in self.editors:
            editor.dispose()

    def openFile(self, fn, quiet=False):
        EditorPanel.openFile(self, fn, quiet)
        self._setFileNames()

    def saveFileAs(self, editor):
        result = EditorPanel.saveFileAs(self, editor)
        self._setFileNames()
        return result

    def _setFileNames(self):
        # the documents adapt their language to the file type
        for editor, filename in self.filenames.items():
            editor.setFileName(filename)

    @pyqtSlot()
    def on_actionPrint_triggered(self):
        printer = QPrinter()
        printer.setOutputFileName('')
        if QPrintDialog(printer, self).exec() == QDialog.DialogCode.Accepted:
            self.currentEditor.print(printer)

    @pyqtSlot()
    def on_actionFind_triggered(self):
        self.currentEditor.setFocus()
        self.monaco.trigger('editor.action.startFindReplaceAction')

    @pyqtSlot()
    def on_actionComment_triggered(self):
        self.monaco.trigger('editor.action.commentLine')

    @pyqtSlot()
    def editSettings(self):
        dlg = MonacoSettingsDialog(self, self.monaco.settings)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.monaco.applySettings(dlg.settings())
            with self.sgroup as settings:
                saveMonacoSettings(settings, self.monaco.settings)
