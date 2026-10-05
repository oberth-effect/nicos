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

"""NICOS tests for the user namespace introspection."""

import json
import sys
import types
from typing import Optional

import pytest

from nicos.commands import helparglist, usercommand, usercommandWrapper
from nicos.core import Param, Readable, anytype, dictof, dictwith, \
    floatrange, intrange, listof, none_or, oneof, setof, tupleof, usermethod
from nicos.core.sessions import introspect
from nicos.core.sessions.introspect import HEADER, SESSION_MODULE, \
    TYPES_MODULE, Stubs

session_setup = 'axis'


class Bar:
    """A result object."""

    count: int

    def value(self) -> float:
        """The value."""

    @property
    def label(self) -> str:
        """The label."""

    @property
    def nolabel(self):
        """No annotation."""


BarList = list[Bar]


class Unhashable:
    """a value of a converter that is not hashable"""

    __hash__ = None

    def __call__(self, val=None):
        return val


class Foo(Readable):
    """A device with annotated user methods."""

    parameters = {
        'gain': Param('The gain', type=float, settable=True, unit='dB'),
        'secret': Param('Internal', type=int, userparam=False),
        # converters, annotated with the type of their values
        'mode': Param('The mode', type=oneof('fast', 'slow')),
        'window': Param('A window', type=tupleof(float, int)),
        'names': Param('Names', type=listof(str)),
        'weights': Param('Weights', type=dictof(str, floatrange(0))),
        'level': Param('A level', type=none_or(intrange(0, 5))),
        'choice': Param('Mixed', type=oneof(1, 'auto')),
        'anything': Param('Anything', type=anytype),
        'flags': Param('Flags', type=setof('a', 1)),
        'options': Param('Options', type=dictwith(a=int)),
        'odd': Param('Odd', type=Unhashable()),
    }

    def doRead(self, maxage=0) -> float:
        """Read the hardware."""

    @usermethod
    def getBar(self) -> Bar:
        """One Bar."""

    @usermethod
    def getBars(self) -> 'list[Bar]':
        """All Bars."""

    @usermethod
    def lookup(self) -> 'dict[str, Bar] | None':
        """Bars by name."""

    @usermethod
    def pair(self) -> tuple[Bar, int]:
        """A Bar with a number."""

    @usermethod
    def nothing(self):
        """No annotation."""

    @usermethod
    def none(self) -> None:
        """Returns nothing."""

    @usermethod
    def aliased(self) -> 'BarList':
        """A type alias of the module."""

    @usermethod
    def optional(self) -> 'Optional[dict[str, Bar]]':
        """Maybe Bars."""

    @usermethod
    def missing(self) -> 'Missing':  # noqa: F821
        """An unknown name."""

    @usermethod
    def scaled(self, factor: float, *rest, flag: bool = False) -> Bar:
        """With parameters."""


class List:
    """A class with the name of a generic of the typing module."""


class Names:
    """Names that commands can shadow."""

    @property
    def number(self) -> int:
        """A builtin."""

    def bars(self) -> 'list[Bar]':
        """A generic."""

    def other(self) -> List:
        """A class."""


@usercommand
@helparglist('dev, pos, ...')
def mymaw(*dev_pos_list):
    """Move.

    More.
    """


@usercommand
@helparglist('fitclass, [[xcol, ]ycol]')
def myfit(fitclass, *args, **kwargs):
    """Fit."""


@usercommand
def readBars(dev: Foo, *, n: int = 3, fmt=object()) -> 'list[Bar]':
    """Read bars."""


def stubsOf(*classes, commands=None):
    """Return the stubs of *classes* and of *commands* (name -> function),
    and the sources of the session and types modules, which must compile."""
    commands = commands or {}
    stubs = Stubs(commands)
    for cls in classes:
        stubs.classname(cls, 0)
    for name, func in commands.items():
        stubs.command(name, func)
    modules = stubs.modules()
    for name, source in modules.items():
        compile(source, name, 'exec')
    return stubs, modules[SESSION_MODULE + '.py'], \
        modules[TYPES_MODULE + '.py']


def test_namespace_info(session):
    for devname in ('motor', 'axis', 'aliasAxis'):
        session.createDevice(devname, explicit=True)
    info = session.getNamespaceInfo()
    assert set(info) == {'commands', 'devices', 'apis', 'stubs'}
    # only plain types are allowed, since the result is sent to clients
    json.dumps(info)

    commands = {cmd['name']: cmd for cmd in info['commands']}
    assert [cmd['name'] for cmd in info['commands']] == \
        sorted(commands, key=str.lower)
    assert commands['maw']['summary'].startswith('Move one or more devices')
    # hidden commands and private names are not included
    assert 'ClearAllCache' not in commands
    assert not any(name.startswith('_') for name in commands)

    devices = {dev['name']: dev for dev in info['devices']}
    motor = devices['motor']
    assert motor['unit'] == 'mm'
    assert motor['alias'] is None
    assert motor['class'] == 'nicos.devices.generic.virtual.VirtualMotor'
    # aliases are described with the class and unit of the aliased device
    alias = devices['aliasAxis']
    assert alias['alias'] == 'axis'
    assert alias['class'] == devices['axis']['class']
    assert alias['unit'] == devices['axis']['unit']
    # devices not exported to the namespace are not included
    assert 'coder' not in devices

    assert set(info['stubs']) == {'nicos_session.py', '_nicos_types.py'}
    for name, source in info['stubs'].items():
        compile(source, name, 'exec')
    session_ = info['stubs']['nicos_session.py']
    types_ = info['stubs']['_nicos_types.py']
    # the session module imports the commands and declares the devices
    assert 'from _nicos_types import maw\n' in session_
    assert 'motor: _nicos_types.VirtualMotor\n' in session_
    assert "aliasAxis: _nicos_types.Axis\n'Alias of axis.'\n" in session_
    # the types module defines the commands and classes
    assert "def maw(dev, pos, *args):\n    'maw(dev, pos, ...)\\n\\nMove " \
        "one or more devices" in types_
    assert "NICOS moveable nicos.devices.generic.virtual.VirtualMotor'\n" \
        in types_
    # parameters with the type of their values
    assert "    @property\n    def speed(self) -> float:\n        'Virtual " \
        "speed of the device\\n\\nSettable parameter, type a float in the " \
        "range" in types_
    assert '    def abslimits(self) -> Tuple[float, float]:\n' in types_
    # all public methods, the user methods and parameters are the API
    assert '    def doStart(self, target):\n' in types_
    assert '    def format(self, value, unit=False):\n' in types_
    api = info['apis']['_nicos_types.VirtualMotor']
    assert {'maw', 'read', 'speed', 'unit'} <= set(api)
    assert not {'doStart', 'format', 'classes'} & set(api)


def test_namespace_jedi(session, tmp_path):
    jedi = pytest.importorskip('jedi')
    for devname in ('motor', 'axis', 'aliasAxis'):
        session.createDevice(devname, explicit=True)
    for name, source in session.getNamespaceInfo()['stubs'].items():
        (tmp_path / name).write_text(source, encoding='utf-8')
    project = jedi.Project(str(tmp_path), added_sys_path=[str(tmp_path)])

    def complete(text):
        lines = (HEADER + text).split('\n')
        return [c.name for c in jedi.Script(HEADER + text, project=project)
                .complete(len(lines), len(lines[-1]))]

    assert {'maw', 'motor', 'move'} <= set(complete('m'))
    assert complete('motor.spe') == ['speed']
    assert complete('motor.doStar') == ['doStart']
    assert complete('aliasAxis.offs') == ['offset']


def test_typed_classes():
    stubs, _, types_ = stubsOf(Foo)
    assert "class Foo:\n    'A device with annotated user methods.\\n\\n" \
        "NICOS readable %s.Foo'\n" % __name__ in types_
    for line in [
        '    def getBar(self) -> Bar:',
        '    def getBars(self) -> List[Bar]:',
        '    def pair(self) -> Tuple[Bar, int]:',
        # string annotations are evaluated, also type aliases
        '    def aliased(self) -> List[Bar]:',
        '    def optional(self) -> Union[Dict[str, Bar], None]:',
        # "X | Y" only from Python 3.10 on, else it is left for jedi
        '    def lookup(self) -> Union[Dict[str, Bar], None]:'
        if sys.version_info >= (3, 10) else
        "    def lookup(self) -> 'dict[str, Bar] | None':",
        "    def missing(self) -> 'Missing':",
        '    def nothing(self):',
        '    def none(self) -> None:',
        '    def scaled(self, factor: float, *rest, flag: bool = False) '
        '-> Bar:',
        # methods for drivers, defined here and inherited
        '    def doRead(self, maxage=0) -> float:',
        '    def doStatus(self, maxage=0):',
    ]:
        assert line + '\n' in types_
    # the user parameters as properties, with the type of their values
    assert "    @property\n    def gain(self) -> float:\n        'The gain" \
        "\\n\\nSettable parameter, type float, unit dB.'\n" in types_
    assert "    @property\n    def unit(self) -> str:\n        'Unit of the " \
        "device main value\\n\\nSettable parameter, type a string.'\n" \
        in types_
    for line in [
        '    def mode(self) -> str:',
        '    def window(self) -> Tuple[float, int]:',
        '    def names(self) -> List[str]:',
        '    def weights(self) -> Dict[str, float]:',
        '    def level(self) -> Union[int, None]:',
        '    def choice(self) -> Union[int, str]:',
        # a set as a list, its types ordered by name
        '    def flags(self) -> List[Union[int, str]]:',
        '    def options(self) -> Dict[str, Any]:',
        # no Python type: no annotation, rather than Any
        '    def anything(self):',
        '    def odd(self):',
    ]:
        assert line + '\n' in types_
    assert 'def secret(' not in types_ and 'def classes(' not in types_
    api = stubs.apis['_nicos_types.Foo']
    assert api == sorted(api)
    assert {'gain', 'getBar', 'scaled', 'read', 'status', 'unit'} <= set(api)
    assert not {'doRead', 'format', 'secret', 'classes'} & set(api)

    # the referenced classes, with their methods, properties and annotated
    # attributes, but not the builtins
    assert "class Bar:\n    'A result object.'\n" in types_
    assert "    def value(self) -> float:\n        'The value.'\n" in types_
    assert "    @property\n    def label(self) -> str:\n        'The " \
        "label.'\n" in types_
    assert "    @property\n    def nolabel(self):\n        'No " \
        "annotation.'\n" in types_
    assert '    @property\n    def count(self) -> int:\n        ...\n' \
        in types_
    assert 'class str' not in types_ and 'class list' not in types_


def test_bounds(monkeypatch):
    monkeypatch.setattr(introspect, 'MAX_DEPTH', 0)
    _, _, types_ = stubsOf(Foo)
    # classes beyond the bounds are unknown
    assert 'class Bar' not in types_
    assert '    def getBars(self) -> List[Any]:\n' in types_
    assert "    def getBar(self) -> '%s.Bar':\n" % __name__ in types_


def test_annotations_of_base_classes():
    """Annotations are evaluated in the module of the class defining them."""
    module = types.ModuleType('introspect_base')
    exec('class Thing:\n    pass\n\n'  # pylint: disable=exec-used
         'class Base:\n    def get(self) -> "Thing":\n        pass\n',
         vars(module))
    sys.modules[module.__name__] = module
    try:
        class Derived(module.Base):
            pass
        _, _, types_ = stubsOf(Derived)
        assert '    def get(self) -> Thing:\n' in types_
        assert 'class Thing:\n' in types_
    finally:
        del sys.modules[module.__name__]


def test_evaluation_without_side_effects():
    module = types.ModuleType('introspect_effects')
    exec('class Thing:\n'  # pylint: disable=exec-used
         '    def get(self) -> "(leaked := Thing)":\n        pass\n',
         vars(module))
    sys.modules[module.__name__] = module
    try:
        _, _, types_ = stubsOf(module.Thing)
        assert '    def get(self) -> Thing:\n' in types_
        assert 'leaked' not in vars(module)
        assert 'leaked' not in vars(module.Thing)
    finally:
        del sys.modules[module.__name__]


def test_commands():
    _, session_, types_ = stubsOf(commands={
        'mymaw': mymaw, 'myfit': myfit, 'readBars': readBars})
    assert 'from _nicos_types import mymaw\n' in session_
    # the help_arglist is the signature if it can be, and else is shown in
    # the docstring, also if it was changed
    assert "def mymaw(dev, pos, *args):\n    'mymaw(dev, pos, ...)\\n\\n" \
        "Move.\\n\\nMore.'\n" in types_
    assert "def myfit(fitclass, *args, **kwargs):\n    'myfit(fitclass, " \
        "[[xcol, ]ycol])\\n\\nFit.'\n" in types_
    # annotations with the names of the stubs, invalid defaults are replaced
    assert "def readBars(dev: Foo, *, n: int = 3, fmt=...) -> List[Bar]:\n" \
        "    'Read bars.'\n" in types_
    assert 'class Foo:\n' in types_ and 'class Bar:\n' in types_
    # wrapped commands are described by their function
    assert Stubs(['mymaw']).command('mymaw', usercommandWrapper(mymaw)) == \
        {'name': 'mymaw', 'summary': 'Move.'}


def test_names():
    def anything(*args):
        pass

    shadowing = ['Bar', 'List', 'int', 'property']
    _, session_, types_ = stubsOf(
        Names, commands=dict.fromkeys(shadowing, anything))
    # commands keep their names
    for name in shadowing:
        assert 'from _nicos_types import %s\n' % name in session_
        assert 'def %s(*args):\n' % name in types_
    # the builtins and generics they shadow are written qualified
    assert '    @builtins.property\n    def number(self) -> builtins.int:\n' \
        in types_
    assert '    def bars(self) -> typing.List[Bar_2]:\n' in types_
    # classes get unique names
    assert '    def other(self) -> List_2:\n' in types_
    assert 'class Bar_2:\n' in types_ and 'class List_2:\n' in types_
