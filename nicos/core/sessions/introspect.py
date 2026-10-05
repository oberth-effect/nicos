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

"""The user namespace as Python stub modules, for script editors.

The user commands and devices exported to the user namespace are written as
two modules, which a client puts on the path of jedi, so that a script
prefixed with :data:`HEADER` is plain Python to it:

* ``_nicos_types`` defines the commands, and a class for each class of a
  device and each class reached from these through type annotations, up to
  a limited distance: the user parameters of devices as properties,
  annotated with the type of their values where the NICOS parameter type
  has one, all public methods, and the attributes of other classes.
* ``nicos_session`` imports the commands and declares the devices with their
  classes.  The types module itself is not star-imported, so that the names
  of classes cannot shadow devices.

Annotations are written with the names of the stubs, containers as the
``List``, ``Dict``, ``Tuple`` and ``Union`` of the typing module, which all
versions of jedi follow; unknown types are written as strings, which jedi
may still resolve, unknown elements of containers as ``Any``.  A
``help_arglist`` is the signature if it is valid Python, else the real
signature is used and the ``help_arglist`` is shown in the docstring.

Nothing in this module executes device or user code: the information is
read only from class and module dictionaries and from the attributes that
the converters of NICOS parameter types store.  Annotations given as
strings are evaluated with ``typing.get_type_hints()``, in the namespace of
the module that defines them.
"""

import ast
import builtins
import inspect
import keyword
import re
import sys
import types
import typing
from collections import abc
from typing import Any, Dict, FrozenSet, List, Optional, Tuple, Union

from nicos.core import params
from nicos.core.device import Device, DeviceAlias, Moveable, Readable
from nicos.utils import formatDocstring

SESSION_MODULE = 'nicos_session'
TYPES_MODULE = '_nicos_types'
# prepended to scripts, so that jedi knows the commands and devices
HEADER = 'from %s import *\n' % SESSION_MODULE

# bounds for the classes reached through annotations
MAX_CLASSES = 400
MAX_DEPTH = 3

_SEQUENCES = (list, set, frozenset, abc.Sequence, abc.MutableSequence,
              abc.Set, abc.MutableSet, abc.Collection, abc.Iterable,
              abc.Iterator, abc.Generator, abc.Reversible)
_MAPPINGS = (dict, abc.Mapping, abc.MutableMapping)
# types.UnionType (X | Y) exists from Python 3.10 on
_UNIONS = (typing.Union, getattr(types, 'UnionType', typing.Union))
_UNKNOWN = (typing.Any, inspect.Parameter.empty)
# typing.Self exists from Python 3.11 on; before, no annotation is _SELF
_SELF = getattr(typing, 'Self', object())
# the generics of the typing module in the stubs
_TYPING = ('Any', 'Dict', 'List', 'Tuple', 'Union')
# a trailing "..." in an argument list stands for more arguments
_MORE_ARGS = re.compile(r'(^|,)\s*\.\.\.$')


def describeNamespace(session):
    """Return the user commands and devices exported to the user namespace
    of *session*, as a dictionary with these keys:

    * ``'commands'`` -- a list of dictionaries with ``'name'`` and
      ``'summary'``, the first line of the docstring
    * ``'devices'`` -- a list of dictionaries with ``'name'``, ``'class'``
      (the qualified name of the class), ``'description'``, ``'unit'`` and
      ``'alias'`` (the name of the aliased device, or None); for an alias,
      the class and unit are those of the aliased device
    * ``'apis'`` -- the user API of the classes of devices, their user methods
      and parameters, by the full names of their stubs as jedi gives them;
      their other methods, for drivers (``doRead()`` etc.) and internal ones,
      are for clients to offer only on request
    * ``'stubs'`` -- the sources of the stub modules, by file name

    The lists are sorted by name.
    """
    commands, devices = [], []
    for name, obj in sorted(session.getExportedObjects(),
                            key=lambda item: item[0].lower()):
        if name.startswith('_') or not _isName(name):
            continue
        if getattr(obj, 'is_usercommand', False):
            if not getattr(obj, 'is_hidden', False) and \
               not getattr(getattr(obj, 'real_func', obj), 'is_hidden', False):
                commands.append((name, obj))
        elif isinstance(obj, Device):
            devices.append((name, obj))
    stubs = Stubs(name for name, _ in commands)
    return {
        'devices': [stubs.device(name, dev) for name, dev in devices],
        'commands': [stubs.command(name, cmd) for name, cmd in commands],
        # writing the classes also collects the APIs
        'stubs': stubs.modules(),
        'apis': stubs.apis,
    }


class Stubs:
    """Writes the stub modules of the commands with the names *commands*,
    the devices, and the classes reached from these.

    Commands keep their names; builtins they shadow are written as
    ``builtins.name``.  Classes get unique names, and are written breadth
    first, within the bounds.
    """

    def __init__(self, commands):
        self.commands = set(commands)
        self.apis = {}       # full name of the stub of a device class -> API
        self._session = []   # lines of the session module
        self._types = []     # lines of the types module for the commands
        self._names = {}     # class -> its name in the stubs
        self._pending = []   # (class, name, depth) to write
        self._taken = set(_TYPING) | self.commands | set(dir(builtins)) | \
            {'builtins', 'typing'}

    def device(self, name, dev):
        """Declare the device *dev* exported as *name*; return the data of
        the device."""
        target = dev
        if issubclass(type(dev), DeviceAlias) and isinstance(dev._obj, Device):
            target = dev._obj
        cls = type(target)
        clsname = self.classname(cls, 0)
        self._session.append('%s: %s.%s' % (
            name, TYPES_MODULE, clsname or 'builtins.object'))
        info = {
            'name': name,
            'class': '%s.%s' % (cls.__module__, cls.__qualname__),
            'description': str(dev._params.get('description') or ''),
            'unit': str(target._params.get('unit') or ''),
            'alias': target.name if target is not dev else None,
        }
        doc = '\n\n'.join(filter(None, [
            info['description'],
            info['alias'] and 'Alias of %s.' % info['alias']]))
        if doc:
            self._session.append(repr(doc))
        return info

    def command(self, name, cmd):
        """Define the user command *cmd* exported as *name*; return the data
        of the command."""
        self._types += [''] + self._function(name, cmd, _Types(self, 1))
        self._session.append('from %s import %s' % (TYPES_MODULE, name))
        return {'name': name,
                'summary': _docOf(cmd).split('\n', 1)[0]}

    def classname(self, cls, depth):
        """Return the name of the class *cls* in the stubs, which writes it
        at *depth*; None if it is beyond the bounds."""
        if cls not in self._names:
            if depth > MAX_DEPTH or len(self._pending) >= MAX_CLASSES:
                return None
            name = cls.__name__
            unique, number = name, 1
            while unique in self._taken:
                number += 1
                unique = '%s_%d' % (name, number)
            self._taken.add(unique)
            self._names[cls] = unique
            self._pending.append((cls, unique, depth))
        return self._names[cls]

    def builtin(self, name):
        """Return how to write the builtin *name* in the types module."""
        return 'builtins.' + name if name in self.commands else name

    def modules(self):
        """Return the sources of the stub modules, by file name."""
        lines = ['"""Classes and commands of the NICOS session, for jedi."""',
                 '', 'import builtins', 'import typing',
                 'from typing import ' + ', '.join(_TYPING)]
        # the list grows while the classes are written
        for cls, name, depth in self._pending:
            lines += [''] + self._class(cls, name, depth)
        session = ['"""Commands and devices of the NICOS session, for '
                   'jedi."""', '', 'import ' + TYPES_MODULE] + self._session
        return {SESSION_MODULE + '.py': '\n'.join(session) + '\n',
                TYPES_MODULE + '.py': '\n'.join(lines + self._types) + '\n'}

    def generic(self, name):
        """Return how to write the generic *name* of the typing module."""
        return 'typing.' + name if name in self.commands else name

    def _class(self, cls, name, depth):
        types_ = _Types(self, depth + 1, cls)
        doc = _formatDoc(cls.__doc__)
        if issubclass(cls, Device):
            kind = 'moveable' if issubclass(cls, Moveable) else \
                'readable' if issubclass(cls, Readable) else 'device'
            doc += '\n\nNICOS %s %s.%s' % (
                kind, cls.__module__, cls.__qualname__)
        members = {}  # name -> lines; the first definition of a name wins
        if issubclass(cls, Device):
            api = []
            for pname, pinfo in sorted(cls.parameters.items()):
                if pinfo.userparam and _isName(pname):
                    api.append(pname)
                    ptype = _valueType(pinfo.type)
                    members[pname] = self._property(
                        pname, None if ptype is Any else types_.text(ptype),
                        _paramDoc(pinfo))
            for mname in sorted(dir(cls)):
                attr = inspect.getattr_static(cls, mname, None)
                if not mname.startswith('_') and _funcOf(attr):
                    # the registry of methods marks the user methods
                    registered = cls.methods.get(mname)
                    if registered and registered[3]:
                        api.append(mname)
                    members.setdefault(mname,
                                       self._method(mname, attr, types_))
            self.apis['%s.%s' % (TYPES_MODULE, name)] = sorted(api)
        else:
            for aname in sorted(dir(cls)):
                attr = inspect.getattr_static(cls, aname, None)
                if aname.startswith('_'):
                    continue
                if isinstance(attr, property):
                    returns = _annotations(attr.fget).get(
                        'return', inspect.Parameter.empty)
                    members[aname] = self._property(
                        aname, types_.inModuleOf(attr.fget).text(returns),
                        _formatDoc(attr.__doc__))
                elif isinstance(attr, (types.GetSetDescriptorType,
                                       types.MemberDescriptorType)):
                    members[aname] = self._property(
                        aname, None, _formatDoc(attr.__doc__))
                elif _funcOf(attr):
                    members[aname] = self._method(aname, attr, types_)
            for base in reversed(cls.__mro__):
                for aname, ann in sorted(_annotations(base).items()):
                    if not aname.startswith('_'):
                        members.setdefault(aname, self._property(
                            aname, types_.inModuleOf(base).text(ann), ''))
        lines = ['class %s:' % name, '    ' + repr(doc.strip())]
        for mname, mlines in members.items():
            if _isName(mname):
                lines += mlines
        return lines

    def _property(self, name, annotation, doc):
        return ['    @' + self.builtin('property'),
                '    def %s(self)%s:' % (name, ' -> ' + annotation
                                         if annotation else ''),
                '        ' + (repr(doc) if doc else '...')]

    def _method(self, name, attr, types_):
        return self._function(name, _funcOf(attr), types_, '    ', attr)

    def _function(self, name, func, types_, indent='', attr=None):
        """Return the lines of the function *func* defined as *name*.  For a
        method, *attr* is the attribute of the class."""
        real_func = getattr(func, 'real_func', func)
        types_ = types_.inModuleOf(real_func)
        arglist = getattr(func, 'help_arglist', None) or \
            getattr(real_func, 'help_arglist', None)
        try:
            signature = inspect.signature(real_func)
        except (TypeError, ValueError):
            signature = None
        lines = []
        first = ''
        if isinstance(attr, (staticmethod, classmethod)):
            lines.append(indent + '@' + self.builtin(type(attr).__name__))
            first = 'cls, ' if isinstance(attr, classmethod) else ''
        elif attr is not None:
            first = 'self, '
        candidates = []
        if arglist:
            candidates.append(first + _MORE_ARGS.sub(
                r'\1 *args', arglist.strip()).strip())
        if signature is not None:
            candidates.append(types_.parameters(signature))
        candidates.append(first + '*args, **kwargs')
        params = next(c for c in candidates if _isSignature(c))
        returns = types_.text(signature.return_annotation) \
            if signature is not None else None
        doc = _docOf(func)
        if arglist and params != first + arglist.strip():
            doc = ('%s(%s)\n\n%s' % (name, arglist.strip(), doc)).strip()
        returns = ' -> ' + returns if returns else ''
        lines += [indent + 'def %s(%s)%s:' % (name, params, returns),
                  indent + '    ' + (repr(doc) if doc else '...')]
        return lines


class _Text(str):
    """Text that inspect writes as it is, for annotations and defaults."""

    def __repr__(self):
        return str(self)


class _Types:
    """Writes annotations for the stubs of *stubs*, registering the classes
    they reference at *depth*.  Annotations given as strings are evaluated
    in the namespace of the module that defines them (see :meth:`inModuleOf`)
    and in that of the *owner* class."""

    def __init__(self, stubs, depth, owner=None, namespace=None):
        self.stubs = stubs
        self.depth = depth
        self.owner = owner
        self.namespace = namespace or {}

    def inModuleOf(self, obj):
        """Return the writer for the annotations of *obj*, which are
        evaluated in the namespace of the module that defines it."""
        return _Types(self.stubs, self.depth, self.owner, _namespace(obj))

    def text(self, ann):
        """Return the annotation *ann* as written in the stubs; one of an
        unknown type as a string, which jedi may still resolve; None if
        there is none."""
        result = self._text(ann)
        if result is None and ann is not inspect.Parameter.empty:
            return repr(ann if isinstance(ann, str)
                        else inspect.formatannotation(ann))
        return result

    def parameters(self, signature):
        """Return the parameters of *signature* as written in the stubs."""
        params = []
        for param in signature.parameters.values():
            annotation = self.text(param.annotation)
            default = param.default
            if default is not param.empty and not _isExpression(repr(default)):
                default = _Text('...')
            params.append(param.replace(
                annotation=_Text(annotation) if annotation else param.empty,
                default=default))
        return str(signature.replace(
            parameters=params,
            return_annotation=inspect.Signature.empty))[1:-1]

    def _text(self, ann):
        if isinstance(ann, typing.ForwardRef):
            ann = ann.__forward_arg__
        if isinstance(ann, str):
            ann = self._evaluate(ann)
            if ann is None:  # failed
                return None
        if ann is None or ann is type(None):
            return 'None'
        if any(ann is unknown for unknown in _UNKNOWN) or \
           isinstance(ann, (str, typing.TypeVar)):
            return None
        if ann is _SELF:
            return self.stubs.classname(self.owner, self.depth) \
                if self.owner is not None else None
        origin = typing.get_origin(ann)
        if origin is not None:
            return self._generic(origin, typing.get_args(ann))
        if isinstance(ann, type):
            if ann.__module__ == 'builtins':
                return self.stubs.builtin(ann.__name__)
            return self.stubs.classname(ann, self.depth)
        return None

    def _generic(self, origin, args):
        generic = self.stubs.generic
        if origin in _UNIONS:
            members = list(dict.fromkeys(filter(None, map(self._text, args))))
            if len(members) > 1:
                return '%s[%s]' % (generic('Union'), ', '.join(members))
            return members[0] if members else None
        if origin is typing.Annotated:
            return self._text(args[0]) if args else None
        if origin is tuple and not (args and args[-1] is Ellipsis):
            return '%s[%s]' % (generic('Tuple'),
                               ', '.join(map(self._element, args)) or '()')
        # missing arguments are unknown
        first, second = (list(args) + [typing.Any] * 2)[:2]
        if origin is tuple or origin in _SEQUENCES:
            return '%s[%s]' % (generic('List'), self._element(first))
        if origin in _MAPPINGS:
            return '%s[%s, %s]' % (generic('Dict'), self._element(first),
                                   self._element(second))
        if isinstance(origin, type) and origin is not type:
            return self.stubs.classname(origin, self.depth)
        return None

    def _element(self, ann):
        return self._text(ann) or self.stubs.generic('Any')

    def _evaluate(self, text):
        """Evaluate a string annotation with ``typing.get_type_hints()``,
        given an object with this annotation alone: one that fails, e.g. for
        an unknown name, or for "X | Y" before Python 3.10, gives None and
        leaves the others alone.  The namespaces are copied, so that not
        even an assignment expression changes them."""
        holder = types.SimpleNamespace(__annotations__={'hint': text.strip()})
        localns = dict(vars(self.owner)) if self.owner is not None else {}
        try:
            return typing.get_type_hints(holder, dict(self.namespace),
                                         localns)['hint']
        except Exception:
            return None


def _paramDoc(info):
    ptype = info.type.__name__ if isinstance(info.type, type) else \
        info.type.__doc__ or '?'
    facts = ['type ' + ptype] + (['unit ' + info.unit] if info.unit else [])
    return '\n\n'.join(filter(None, [
        info.description, info.ext_desc,
        '%s parameter, %s.' % ('Settable' if info.settable else 'Read-only',
                               ', '.join(facts))]))


def _wrappedType(conv):
    """Return the type of the values of the converter that *conv* wraps."""
    return _valueType(conv.conv)


def _listType(conv):
    """Return the type of the values of the list converter *conv*."""
    return List[_wrappedType(conv)]


def _unionOf(values):
    """Return the union of the distinct types of *values*, ordered by name,
    so that a set of values always gives the same; ``Any`` if there are
    none."""
    kinds = tuple(sorted(set(map(type, values)),
                         key=lambda kind: (kind.__name__, kind.__module__)))
    return Union[kinds] if kinds else Any


def _optional(ann):
    """Return the optional *ann*; ``Any`` stays unknown, since the stubs
    leave out the unknown members of unions."""
    return Any if ann is Any else Optional[ann]


# the types of the values of NICOS parameter types, keyed by the converter
# functions and by the classes of the converters with a fixed type
# (subclasses of the converters listed here and below are not matched, they
# give Any) ...
_VALUE_TYPES = {
    params.string: str, params.boolean: bool, params.nonemptystring: str,
    params.nicosdev: str, params.tangodev: str, params.pvname: str,
    params.mailaddress: str, params.absolute_path: str,
    params.relative_path: str, params.expanded_path: str, params.subdir: str,
    params.ipv4: str, params.limits: Tuple[float, float],
    params.vec3: List[float], params.intrange: int, params.floatrange: float,
    params.host: str, params.dictwith: Dict[str, Any],
}
# ... and the rules for those built from other converters or values, keyed
# by the class
_CONVERTERS = {
    params.listof: _listType,
    params.nonemptylistof: _listType,
    params.tupleof: lambda conv: Tuple[tuple(map(_valueType, conv.types))],
    params.dictof: lambda conv: Dict[_valueType(conv.keyconv),
                                     _valueType(conv.valconv)],
    # the stub writer writes a frozenset as a list
    params.setof: lambda conv: FrozenSet[_unionOf(conv.vals)],
    params.oneof: lambda conv: _unionOf(conv.vals),
    params.oneofdict: lambda conv: _unionOf(conv.vals.values()),
    params.oneofdict_or: _wrappedType,
    params.nonzero: _wrappedType,
    params.none_or: lambda conv: _optional(_wrappedType(conv)),
}


def _valueType(conv):
    """Return the type of the values of the NICOS parameter type *conv*,
    ``Any`` if unknown.  Converters are not called, only the attributes that
    they store are read."""
    if isinstance(conv, type):
        return conv
    # converters are functions, or instances of converter classes, which
    # need not be hashable
    key = conv if isinstance(conv, types.FunctionType) else type(conv)
    if key in _VALUE_TYPES:
        return _VALUE_TYPES[key]
    rule = _CONVERTERS.get(key)
    return rule(conv) if rule else Any


def _namespace(obj):
    """Return the namespace of the module that defines *obj*."""
    module = sys.modules.get(getattr(obj, '__module__', None) or '')
    return vars(module) if module is not None else {}


def _funcOf(attr):
    """Return the function behind a class attribute, or None."""
    if isinstance(attr, (staticmethod, classmethod)):
        attr = attr.__func__
    if callable(attr) and not isinstance(attr, type):
        return attr
    return None


def _annotations(obj):
    try:
        if hasattr(inspect, 'get_annotations'):
            return inspect.get_annotations(obj)
        # before Python 3.10: for classes, without the inherited annotations
        if isinstance(obj, type):
            return dict(obj.__dict__.get('__annotations__', {}))
        return dict(getattr(obj, '__annotations__', None) or {})
    except Exception:
        return {}


def _formatDoc(doc):
    return '\n'.join(formatDocstring(doc or '')).strip()


def _docOf(func):
    """The docstring of a command or method, as NICOS shows it in help."""
    real_func = getattr(func, 'real_func', func)
    return _formatDoc(getattr(func, 'help_doc', None) or real_func.__doc__ or
                      func.__doc__)


def _isName(name):
    return isinstance(name, str) and name.isidentifier() and \
        not keyword.iskeyword(name)


def _isExpression(text):
    try:
        compile(text, '<stub>', 'eval')
    except (SyntaxError, ValueError):
        return False
    return True


def _isSignature(params):
    """Whether *params* is a valid parameter list, and nothing more."""
    try:
        tree = ast.parse('def f(%s): pass' % params)
        compile(tree, '<stub>', 'exec')  # e.g. for duplicate parameters
    except (SyntaxError, ValueError):
        return False
    return len(tree.body) == 1 and len(tree.body[0].body) == 1
