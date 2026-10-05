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

"""NICOS test lib: Test the code assist of the GUI."""

import logging
from os import path

import pytest

from nicos.clients.gui.completion import Catalog, CodeAssist, \
    jediCompletions, statusStyle, writeStubs
from nicos.core import status
from nicos.guisupport.colors import colors
from nicos.guisupport.qt import QObject, pyqtSignal

pytest.importorskip('pytestqt')

# the stub modules as the daemon writes them, see test_introspect
TYPES = r'''"""Classes and commands of the NICOS session, for jedi."""

import builtins
import typing
from typing import Any, Dict, List, Tuple, Union

class VirtualMotor:
    'A class.\n\nNICOS moveable virtual.VirtualMotor'
    @property
    def speed(self) -> float:
        'Speed\n\nSettable parameter, type float, unit main/s.'
    @property
    def comment(self):
        'A comment\n\nSettable parameter, type any value.'
    @property
    def note(self) -> Union[str, None]:
        'A note\n\nSettable parameter, type None or a string.'
    def doRead(self, maxage=0) -> float:
        'Read the hardware.'
    def format(self, value):
        'Format.'
    def getBars(self) -> List[Bar]:
        'Bars.'
    def maw(self, target):
        'Go.'
    def pair(self) -> Tuple[Bar, int]:
        ...

class Bar:
    'A result.'
    @property
    def label(self) -> str:
        'The label.'
    def value(self, factor: float = 1.0) -> float:
        'The value.'

def maw(dev, pos, *args):
    'maw(dev, pos, ...)\n\nMove.'

def readBars(dev: VirtualMotor, *, n: int = 3) -> List[Bar]:
    'Read bars.'
'''

SESSION = r'''"""Commands and devices of the NICOS session, for jedi."""

import _nicos_types
Bar: _nicos_types.Bar
mot: _nicos_types.VirtualMotor
'Alias of motor.'
motor: _nicos_types.VirtualMotor
'a motor'
from _nicos_types import maw
from _nicos_types import readBars
'''

INFO = {
    'commands': [{'name': 'maw', 'summary': 'Move.'},
                 {'name': 'readBars', 'summary': 'Read bars.'}],
    'devices': [
        # a device with the name of a class
        {'name': 'Bar', 'class': 'things.Bar', 'description': '',
         'unit': '', 'alias': None},
        {'name': 'mot', 'class': 'virtual.VirtualMotor',
         'description': '', 'unit': 'mm', 'alias': 'motor'},
        {'name': 'motor', 'class': 'virtual.VirtualMotor',
         'description': 'a motor', 'unit': 'mm', 'alias': None},
    ],
    # the user methods and parameters; the methods for drivers and internal
    # ones are offered only once a name is typed
    'apis': {'_nicos_types.VirtualMotor': ['getBars', 'maw', 'pair',
                                           'speed']},
    'stubs': {'nicos_session.py': SESSION, '_nicos_types.py': TYPES},
}


class FakeClient(QObject):
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    setup = pyqtSignal(object)
    device = pyqtSignal(object)
    isconnected = True

    def __init__(self):
        QObject.__init__(self)
        self.asked = []

    def eval(self, expr, default=None):
        assert expr == 'session.getNamespaceInfo()'
        return INFO

    def ask(self, command, *args, **kwds):
        assert command == 'complete'
        self.asked.append(args)
        return ['%s.target' % args[1].split('.')[0]]

    def getDeviceParams(self, devname):
        return {'value': 1.5, 'target': 2, 'unit': 'mm', 'fmtstr': '%.2f',
                'status': (200, 'idle'), 'userlimits': (-50, 50)}


@pytest.fixture
def assist(qapp):
    colors.init_palette(qapp.palette())
    client = FakeClient()
    catalog = Catalog(client, logging.getLogger('test'))
    catalog.refresh()
    return CodeAssist(catalog)


@pytest.fixture
def stubdir(tmp_path):
    writeStubs(str(tmp_path), INFO['stubs'])
    return str(tmp_path)


def labels(items):
    return [item['label'] for item in items]


def complete(stubdir, text):
    """The names jedi completes at the end of *text*, without private
    ones."""
    lines = text.split('\n')
    return [c.name for c in jediCompletions(stubdir, text, len(lines),
                                            len(lines[-1]))
            if not c.name.startswith('_')]


def test_catalog(qapp):
    catalog = Catalog(FakeClient(), logging.getLogger('test'))

    def stub(name):
        with open(path.join(catalog.stubdir, name), encoding='utf-8') as fp:
            return fp.read()

    # empty stub modules before the first fetch
    assert not catalog.available
    assert stub('nicos_session.py') == stub('_nicos_types.py') == ''
    catalog.refresh()
    assert catalog.available
    assert set(catalog.devices) == {'motor', 'mot', 'Bar'}
    assert set(catalog.commands) == {'maw', 'readBars'}
    assert stub('nicos_session.py') == SESSION
    assert stub('_nicos_types.py') == TYPES
    assert catalog.apis == {'_nicos_types.VirtualMotor': frozenset(
        ['getBars', 'maw', 'pair', 'speed'])}
    changes = []
    catalog.changed.connect(lambda: changes.append(len(catalog.devices)))
    catalog.clear()
    assert not catalog.available and changes == [0]
    assert stub('nicos_session.py') == ''


def test_write_stubs(tmp_path):
    # only the files of the stub modules are written
    writeStubs(str(tmp_path), {'nicos_session.py': 'x = 1\n',
                               '../escape.py': 'y = 2\n'})
    assert sorted(p.name for p in tmp_path.iterdir()) == \
        ['_nicos_types.py', 'nicos_session.py']
    assert (tmp_path / 'nicos_session.py').read_text() == 'x = 1\n'


def test_jedi(stubdir):
    pytest.importorskip('jedi')
    # jedi offers all members, the front ends mark and filter them
    assert complete(stubdir, 'motor.') == ['comment', 'doRead', 'format',
                                           'getBars', 'maw', 'note', 'pair',
                                           'speed']
    # the results of annotated methods, their elements and their attributes
    assert complete(stubdir, 'motor.getBars()[0].') == ['label', 'value']
    assert complete(stubdir, 'motor.pair()[0].') == ['label', 'value']
    assert 'bit_length' in complete(stubdir, 'motor.pair()[1].')
    assert complete(stubdir, 'motor.getBars()[0].label.up') == ['upper']
    # through assignments, loops and commands
    assert complete(stubdir, 'x = motor\nx.sp') == ['speed']
    assert complete(stubdir, 'for b in readBars(motor):\n    b.') == \
        ['label', 'value']
    assert complete(stubdir, 'b, n = motor.pair()\nb.') == ['label', 'value']
    # a device with the name of a class is the device
    assert complete(stubdir, 'Bar.') == ['label', 'value']
    assert {'maw', 'motor', 'mot'} <= set(complete(stubdir, 'm'))


def test_members(assist):
    """The members of devices, and whether they belong to the user API."""
    pytest.importorskip('jedi')
    members = assist.members
    assert members('motor.', 1, 6) == {
        'getBars': True, 'maw': True, 'pair': True, 'speed': True,
        'comment': False, 'doRead': False, 'format': False, 'note': False}
    assert members('x = motor\nx.f', 2, 3) == {'format': False}
    # the members of other values are no business of the catalog
    assert members('x = motor.getBars()[0]\nx.', 2, 2) == {}
    assert members('Bar.', 1, 4) == {}


def test_device_hover(assist):
    def hover(before, word):
        text = before + word
        return assist.hover(text, 1, len(text) - 1, before, word)

    device = hover('maw(', 'motor')
    assert device.startswith('```python\nmotor: VirtualMotor\n```\n\n'
                             '<p>a motor</p>')
    assert '<b>Value:</b> 1.50 mm · <b>Target:</b> 2.00 mm' in device
    assert '<b>Status:</b> <span style="%s">ok: idle</span>' % \
        statusStyle(status.OK) in device
    assert '<b>Limits:</b> [-50.00, 50.00] mm' in device
    assert hover('', 'mot').startswith(
        '```python\nmot: VirtualMotor  # alias of motor\n```')
    # not for attributes and commands
    assert hover('x.', 'motor') is None
    assert hover('', 'maw') is None
    # without live values the hover comes from the catalog only
    assist.livevalues = False
    device = hover('', 'motor')
    assert '<b>Value:</b>' not in device and '<b>Unit:</b> mm' in device


def test_type_hover(assist):
    """The type of other values, which the language server does not show,
    and the docstring of names that jedi infers no value or several for,
    which the language server does not document."""
    pytest.importorskip('jedi')

    def hover(text, before, word):
        lines = text.split('\n')
        return assist.hover(text, len(lines), len(lines[-1]) - 1,
                            before, word)

    assert hover('lst = motor.getBars()\nlst', '', 'lst') == \
        '```python\nlst: list[Bar]\n```'
    assert hover('x = motor.getBars()[0]\nx', '', 'x') == \
        '```python\nx: Bar\n```'
    assert hover('for b in readBars(motor):\n    b', '    ', 'b') == \
        '```python\nb: Bar\n```'
    assert hover('motor.speed', 'motor.', 'speed') == \
        '```python\nspeed: float\n```'
    assert hover('y = 1 if z else "a"\ny', '', 'y') == \
        '```python\ny: int | str\n```'
    # names without a value, like a parameter whose type has no Python
    # equivalent, show their docstring
    assert hover('motor.comment', 'motor.', 'comment') == \
        '```python\ncomment\n```\n\n<p>A comment</p>\n\n' \
        '<p>Settable parameter, type any value.</p>'
    # names with several values, like a parameter of a union type, show
    # their type and their docstring
    assert hover('motor.note', 'motor.', 'note') == \
        '```python\nnote: None | str\n```\n\n<p>A note</p>\n\n' \
        '<p>Settable parameter, type None or a string.</p>'
    # functions are left to the language server, also one of several that
    # has the name, unknown values are not shown
    assert hover('motor.getBars', 'motor.', 'getBars') is None
    assert hover('if x:\n    def f():\n        "F1."\nelse:\n'
                 '    def f():\n        "F2."\nf', '', 'f') is None
    assert hover('unknown', '', 'unknown') is None


def test_daemon_members(assist):
    members = assist.daemonMembers
    client = assist.client
    # members of other names are asked from the daemon, with the text before
    # the cursor and the dotted name
    assert members('x = s.ta', 1, 8) == [{
        'label': 'target', 'detail': 'from the NICOS session',
        'kind': 'field', 'insert': 'target', 'snippet': False}]
    assert client.asked == [('x = s.ta', 's.ta')]
    client.asked.clear()
    # not for commands and devices, or for other expressions
    assert members('motor.', 1, 6) == []
    assert members('maw.', 1, 4) == []
    assert members('f().x.', 1, 6) == []
    assert members('x', 1, 1) == []
    assert client.asked == []
    assist.daemoncomplete = False
    assert members('s.', 1, 2) == []
    assist.daemoncomplete = True
    client.isconnected = False
    assert members('s.', 1, 2) == []


def test_daemon_members_jedi(assist):
    pytest.importorskip('jedi')
    members = assist.daemonMembers
    # names that jedi knows are left to the language server
    assert members('x = motor\nx.', 2, 2) == []
    assert members('import os\nos.pa', 2, 5) == []
    assert assist.client.asked == []
    assert labels(members('x = unknown()\nx.', 2, 2)) == ['target']


def test_status_style(qapp):
    colors.init_palette(qapp.palette())
    assert statusStyle(status.OK) == 'color:%s;' % colors.dev_fg_ok.name()
    assert statusStyle(status.ERROR) == 'color:%s;background-color:%s;' % (
        colors.text.name(), colors.dev_bg_error.name())
    assert statusStyle(status.UNKNOWN).startswith('color:#')
    assert 'background' not in statusStyle(status.UNKNOWN)
