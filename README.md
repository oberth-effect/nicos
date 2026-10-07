# Better autocomplete and Monaco editor for NICOS (WIP)
[See more detailed but LLM generated description](./llm-monaco-editor.md)

## Goals

- LSP-like support for NICOS script editing,
- familiar editor experience for our users (used to VSCode),
- same autocomplete in console and editor,

## Dependencies
Optional, checked at import time, GUI only.

- `qtmonaco` for the editor (needs Python 3.10+ and PyQt6 with WebEngine, brings `python-lsp-server`, which runs as a subprocess of the GUI),
- `jedi` for the autocomplete,
- Qt6 GUI runtime for the editor (`qtmonaco` requires it); the console autocomplete works with Qt5 too,

## Basic architecture
The session gets an introspection method that can generate all the necessary information for the autocomplete.
Since the autocomplete is done through `jedi`, the introspection is essentially a python stub generator.
Might be worth looking at some actual stub generator instead of the handbuilt one (would require a new core dependency).
Parameter types are derived from the NICOS converters (e.g. `floatrange` to `float`, `none_or(...)` to `Optional`), so parameters get typed hovers and value completion without type hints in device code.

The GUI runs the code assist.
It gets the stubs from the daemon and runs `jedi` on them.
In the editor, the autocomplete, hover help and signature help are done through the LSP, with fallback to the `complete` command for names only the daemon knows.
The console input line runs `jedi` in the GUI process for members after a dot, everything else still goes to the `complete` command.

The `Monaco` editor is the same as VSCode uses, therefore, the editing experience is very familiar to our users.
One editor view is shared by all tabs; it adds a live syntax check, comment toggling with `##`, and `.txt` scripts as plain text.
Currently the settings panel exposes only some basic settings (stored per user), but can be configured some more.
A watchdog restarts the language server when it dies.

Changed files:
- `nicos/core/sessions/introspect.py` (new): writes the stub modules of the user namespace and the data for hovers, reads class dictionaries only, executes no device code.
- `nicos/core/sessions/__init__.py`: adds `Session.getNamespaceInfo()`, the only hook in the daemon, called by the GUI through the existing `eval` command.
- `nicos/guisupport/qt.py`: exports `QWebEnginePage` and `QWebEngineScript` for the editor.
- `nicos/clients/gui/completion.py` (new): `Catalog` (one per GUI client) fetches the stubs and writes them for `jedi`; `CodeAssist` adds hover types, descriptions, cached values and the members known to the daemon only.
- `nicos/clients/gui/widgets/monacoeditor.py` (new): the shared Monaco view, its documents (the tab pages), the settings dialog and the language server watchdog.
- `nicos/clients/gui/widgets/monacoeditor.js` (new): script injected into the Monaco page, keeps the documents and adds the hover and completion providers.
- `nicos/clients/gui/panels/monacoeditor.py` (new): `MonacoEditorPanel`, a subclass of the unchanged `EditorPanel`.
- `nicos/clients/gui/panels/console.py`, `commandline.py`, `cmdbuilder.py`: the `completion='jedi'` option for their input line.
- `requirements-opt.txt`: the optional `qtmonaco` and `jedi`; `INSTALL`: `qtmonaco`.
- tests: `test/test_simple/test_introspect.py`, `test/test_gui/test_completion.py`, `test/test_gui/test_monacoeditor.py`, `test_namespace_info` in `test/test_daemon/test_client.py`.

# Usage

Editor:
```python
panel('nicos.clients.gui.panels.monacoeditor.MonacoEditorPanel')
```

console:
```python
panel('nicos.clients.gui.panels.console.ConsolePanel', completion='jedi')
# or
panel('nicos.clients.gui.panels.commandline.CommandLinePanel', completion='jedi')
# or
panel('nicos.clients.gui.panels.cmdbuilder.CommandPanel', completion='jedi')
```


## LSP features

- NICOS command autocomplete,
- NICOS device and parameter autocomplete,
    - user methods listed by default,
    - non-user methods are listed only if the input matches,
    - on hover show device value, target, status and limits, and parameter value.
- Type inference for getters, list members etc.
  - done by `jedi`: in the LSP for the autocomplete, in the GUI process for the type shown on hover, works best with type-hinted code (we are trying to use type hints in our projects),
- On hover docstrings,
- completion categories (ie. 'NICOS command', 'parameter', 'user method' ...)


## Limitations
- Potentially large stubs sent via the daemon,
- No stub caching, each GUI client calls the introspection command on connect, and whenever device or setup changes.
- `qtmonaco` is a potentially unstable library,
- the editor only works with Qt6 GUI runtime and Python 3.10+,
- the console gets `jedi` completion for members only, no hover or signature help,
- names only the daemon knows are completed by the daemon's completer, which seems to execute parameter getters and may? read volatile parameters from hardware - existing `complete` behaviour (can be switched off in the settings, as the cache reads for hovers),
- hints for 3rd party libraries work only for those installed in the GUI.


## Other architectures considered
- GUI changes only, the GUI sends an introspection script to the daemon,
  - feels better to have a consistent introspect command,
- Introspect generates a structured json that is sent to the GUI,
  - Unnecessary python -> JSON -> python roundtrip,
- The LSP runs at the daemon side.
  - potentially more efficient,
  - new daemon dependency,
  - potentially issues with networking
