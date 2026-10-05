/*
 * NICOS script editor support for the qtmonaco page.
 *
 * Injected into the page before its onload handler runs.  Keeps one Monaco
 * model per open script of the editor panel.  Completion, hover and
 * signature help come from the language server of qtmonaco, which knows the
 * NICOS commands and devices from stub modules; the providers registered
 * here add what only the Python side (the code assist of the host widget)
 * knows: the current values of a device in its hover, and the members of
 * names that only the daemon knows.  Monaco merges their results with those
 * of the language server.
 *
 * Python calls window.nicosEditor.handle({cmd: ..., model: ..., ...}) and
 * queries text() and isModified(), which return JSON.  The page sends events
 * and requests to Python through the "nicos" object on the web channel (the
 * MonacoBridge of the host), which answers the requests with its "answered"
 * signal.
 */
(function () {
  'use strict';
  if (window.nicosEditor) {
    return;
  }

  var REQUEST_TIMEOUT = 4000;
  var CHANGE_DELAY = 400;  // ms after the last change before the text is sent

  var nicos = {
    bridge: null,
    providersInstalled: false,
    comment: '##',                                   // prefix of the comment action
    modelOptions: {tabSize: 4, insertSpaces: true},  // options of each model
    models: new Map(),    // id -> {id, model, viewState, listener, saved, modified, timer}
    currentId: null,
    scratch: null,        // empty model shown when there is no document
    pending: new Map(),   // request id -> {resolve, timer}
    nextRequestId: 1,
    names: {},            // NICOS command or device -> {description, detail}
    wrappedProviders: 0,  // completion providers of qtmonaco wrapped
  };
  window.nicosEditor = nicos;

  // qtmonaco keeps its QWebChannel private: wrap the constructor to get the
  // channel, which also has the bridge of the host.
  var RealQWebChannel = window.QWebChannel;
  window.QWebChannel = function (transport, callback) {
    return new RealQWebChannel(transport, function (channel) {
      // the editor exists now, and qtmonaco connects to the language server
      // only after this callback
      wrapRegistration();
      nicos.bridge = channel.objects.nicos || null;
      if (nicos.bridge) {
        nicos.bridge.answered.connect(function (id, result) {
          resolveRequest(id, JSON.parse(result));
        });
      }
      if (callback) {
        callback(channel);
      }
    });
  };

  function monaco() {
    return window.qtmonaco && window.qtmonaco.monaco;
  }

  function editor() {
    return window.qtmonaco && window.qtmonaco.editor;
  }

  // -- communication with Python ---------------------------------------------

  function send(type, payload) {
    if (!nicos.bridge) {
      return;
    }
    nicos.bridge.notify(JSON.stringify(Object.assign({type: type}, payload)));
  }

  // Ask Python for something; resolves with null on timeout or when offline.
  function request(kind, payload) {
    if (!nicos.bridge) {
      return Promise.resolve(null);
    }
    return new Promise(function (resolve) {
      var id = nicos.nextRequestId++;
      var timer = setTimeout(function () { resolveRequest(id, null); }, REQUEST_TIMEOUT);
      nicos.pending.set(id, {resolve: resolve, timer: timer});
      nicos.bridge.request(id, kind, JSON.stringify(payload));
    });
  }

  function resolveRequest(id, result) {
    var waiting = nicos.pending.get(id);
    if (waiting) {
      clearTimeout(waiting.timer);
      nicos.pending.delete(id);
      waiting.resolve(result);
    }
  }

  // -- providers --------------------------------------------------------------

  var KINDS = {method: 'Method', field: 'Field'};

  function markdown(value) {
    return {value: value, supportHtml: true};
  }

  function inStringOrComment(model, position) {
    var tokens = monaco().editor.tokenize(model.getLineContent(position.lineNumber), model.getLanguageId())[0] || [];
    var type = '';
    for (var i = 0; i < tokens.length && tokens[i].offset < position.column - 1; i++) {
      type = tokens[i].type;
    }
    return /^(string|comment)/.test(type);
  }

  function isMember(model, position) {
    var word = model.getWordUntilPosition(position);
    return /\.\s*$/.test(model.getLineContent(position.lineNumber).slice(0, word.startColumn - 1));
  }

  // Insert *text*, a snippet with the cursor between the brackets, and open
  // the parameter hints: a call.
  function insertCall(item, text) {
    item.insertText = text;
    item.insertTextRules = monaco().languages.CompletionItemInsertTextRule.InsertAsSnippet;
    item.command = {id: 'editor.action.triggerParameterHints', title: 'Parameter hints'};
  }

  function completionItem(item, range) {
    var m = monaco();
    var result = {
      label: item.label,
      kind: m.languages.CompletionItemKind[KINDS[item.kind] || 'Text'],
      detail: item.detail || undefined,
      insertText: item.insert,
      range: range,
    };
    if (item.snippet) {
      insertCall(result, item.insert);
    }
    return result;
  }

  // Members of a dotted name from the daemon; Python answers only for names
  // that the language server does not know.  The keys of a request are the
  // parameters of the CodeAssist methods; Python counts columns from 0.
  function provideCompletionItems(model, position) {
    // after a dot; Python decides which dotted names qualify
    if (!isMember(model, position) || inStringOrComment(model, position)) {
      return {suggestions: []};
    }
    var word = model.getWordUntilPosition(position);
    var range = new (monaco().Range)(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn);
    return request('complete', {line: position.lineNumber, column: position.column - 1, text: model.getValue()}).then(function (items) {
      return {suggestions: (items || []).map(function (item) { return completionItem(item, range); })};
    });
  }

  // The type of a value, and the description and current values of a device.
  function provideHover(model, position) {
    if (inStringOrComment(model, position)) {
      return null;
    }
    var word = model.getWordAtPosition(position);
    if (!word) {
      return null;
    }
    var range = new (monaco().Range)(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn);
    var before = model.getLineContent(position.lineNumber).slice(0, word.startColumn - 1);
    return request('hover', {line: position.lineNumber, column: position.column - 1,
                             before: before, word: word.word, text: model.getValue()}).then(function (hover) {
      return hover ? {range: range, contents: [markdown(hover)]} : null;
    });
  }

  function installProviders() {
    var m = monaco();
    if (!m || nicos.providersInstalled) {
      return;
    }
    nicos.providersInstalled = true;
    nicos.providers = {
      completion: {triggerCharacters: ['.'], provideCompletionItems: provideCompletionItems},
      hover: {provideHover: provideHover},
    };
    applyComment();
    wrapRegistration();
    m.languages.registerCompletionItemProvider('python', nicos.providers.completion);
    m.languages.registerHoverProvider('python', nicos.providers.hover);
  }

  // -- the completion items of the language server ----------------------------
  // qtmonaco hands them to Monaco as they come.  Its provider, registered
  // whenever it connects to the server, is wrapped to give the items the
  // kinds of Monaco, which are numbered differently, and the NICOS commands
  // and devices a description and a detail from the catalog (nicos.names).
  // Of the members of devices, Python tells which belong to the user API
  // (user methods and parameters): these are marked, the others are offered
  // only once a name is typed.  Private members of any value come only for a
  // typed "_".

  var LSP_KINDS = [null, 'Text', 'Method', 'Function', 'Constructor', 'Field', 'Variable', 'Class',
                   'Interface', 'Module', 'Property', 'Unit', 'Value', 'Enum', 'Keyword', 'Snippet',
                   'Color', 'File', 'Reference', 'Folder', 'EnumMember', 'Constant', 'Struct', 'Event',
                   'Operator', 'TypeParameter'];

  // members: for completions after a dot, member of a device -> whether in the user API
  function adaptItems(model, position, result, members) {
    if (!result || !result.suggestions) {
      return result;
    }
    var m = monaco();
    var typed = model.getWordUntilPosition(position).word;
    var member = isMember(model, position);
    result.suggestions = result.suggestions.filter(function (item) {
      if (item.range) {  // the snippets of qtmonaco are made for Monaco
        return true;
      }
      var kind = LSP_KINDS[item.kind];
      if (kind) {
        item.kind = m.languages.CompletionItemKind[kind];
      }
      var label = typeof item.label === 'string' ? item.label : item.label.label;
      var name = label.split('(')[0];
      if (kind === 'Method' || kind === 'Function' || kind === 'Constructor') {
        // the client of qtmonaco declares no snippet support, so the language
        // server inserts the bare name: insert a call, as for the members
        // from the daemon
        insertCall(item, name + '($0)');
      }
      if (member) {
        var api = members && members[name];
        // private members once the typed name begins with "_", as in the
        // console; asked again for the next character
        if (name.charAt(0) === '_' && typed.charAt(0) !== '_' || api === false && !typed) {
          result.incomplete = result.incomplete || !typed;
          return false;
        }
        if (api) {
          item.label = {label: label, description: kind === 'Property' ? 'parameter' : 'user method'};
        }
      } else if (nicos.names[name]) {
        item.label = {label: label, description: nicos.names[name].description};
        item.detail = nicos.names[name].detail || undefined;
      }
      return true;
    });
    return result;
  }
  nicos.adaptItems = adaptItems;

  function wrapRegistration() {
    var languages = monaco() && monaco().languages;
    if (!languages || languages.nicosRegister) {
      return;
    }
    var register = languages.nicosRegister = languages.registerCompletionItemProvider;
    languages.registerCompletionItemProvider = function (selector, provider) {
      if (nicos.providers && provider === nicos.providers.completion) {
        return register.call(this, selector, provider);
      }
      nicos.wrappedProviders++;
      return register.call(this, selector, Object.assign({}, provider, {
        provideCompletionItems: function (model, position, context, token) {
          var result = provider.provideCompletionItems(model, position, context, token);
          return Promise.resolve(result).then(function (items) {
            if (!items || !isMember(model, position)) {
              return adaptItems(model, position, items, null);
            }
            return request('members', {line: position.lineNumber,
                                       column: position.column - 1, text: model.getValue()}).then(function (members) {
              return adaptItems(model, position, items, members);
            });
          });
        },
      }));
    };
  }

  // -- settings ---------------------------------------------------------------

  // Editor options; the indentation is an option of the models.
  function updateOptions(options) {
    if (options.tabSize !== undefined) {
      nicos.modelOptions.tabSize = options.tabSize;
      nicos.models.forEach(function (item) {
        item.model.updateOptions(nicos.modelOptions);
      });
    }
    editor().updateOptions(options);
  }

  // The prefix of the "comment line" action; Monaco adds the space.
  function applyComment() {
    var m = monaco();
    if (m) {
      m.languages.setLanguageConfiguration('python', {comments: {lineComment: nicos.comment}});
    }
  }

  // -- models -----------------------------------------------------------------

  function entry(id) {
    return nicos.models.get(id) || null;
  }

  function isCurrent(item) {
    var ed = editor();
    return !!(ed && item && ed.getModel() === item.model);
  }

  function endPosition(model) {
    var last = model.getLineCount();
    return new (monaco().Position)(last, model.getLineMaxColumn(last));
  }

  function createModel(id) {
    var m = monaco();
    if (nicos.models.has(id)) {
      return;
    }
    var model = m.editor.createModel('', 'python');
    model.setEOL(m.editor.EndOfLineSequence.LF);
    model.updateOptions(nicos.modelOptions);
    var item = {id: id, model: model, viewState: null, modified: false, timer: null,
                saved: model.getAlternativeVersionId()};
    // the modified state is reported at once, the text for the syntax check
    // after a pause in typing
    item.listener = model.onDidChangeContent(function () {
      updateModified(item);
      clearTimeout(item.timer);
      item.timer = setTimeout(function () {
        send('changed', {model: id, text: model.getValue()});
      }, CHANGE_DELAY);
    });
    nicos.models.set(id, item);
  }

  // A model is modified unless it is at the saved version, which undo can
  // also lead back to.
  function updateModified(item) {
    var modified = item.model.getAlternativeVersionId() !== item.saved;
    if (modified !== item.modified) {
      item.modified = modified;
      send('modified', {model: item.id, modified: modified});
    }
  }

  function setModified(item, modified) {
    item.saved = modified ? null : item.model.getAlternativeVersionId();
    updateModified(item);
  }

  function showModel(id) {
    var ed = editor();
    var previous = entry(nicos.currentId);
    if (previous && isCurrent(previous)) {
      previous.viewState = ed.saveViewState();
    }
    var item = entry(id);
    if (!item) {
      if (!nicos.scratch || nicos.scratch.isDisposed()) {
        nicos.scratch = monaco().editor.createModel('', 'python');
      }
      ed.setModel(nicos.scratch);
      ed.updateOptions({readOnly: true});
      nicos.currentId = null;
      return;
    }
    ed.setModel(item.model);
    ed.updateOptions({readOnly: false});
    nicos.currentId = id;
    if (item.viewState) {
      ed.restoreViewState(item.viewState);
    }
  }

  function disposeModel(id) {
    var item = entry(id);
    if (!item) {
      return;
    }
    if (nicos.currentId === id) {
      showModel(null);
    }
    clearTimeout(item.timer);
    item.listener.dispose();
    item.model.dispose();
    nicos.models.delete(id);
  }

  // setValue() gives a text without line breaks the end of line sequence of
  // the platform, CRLF on Windows: keep LF, as the text on the Python side.
  function setText(item, text) {
    item.model.setValue(text);
    item.model.setEOL(monaco().editor.EndOfLineSequence.LF);
  }

  // Insert at the cursor if the model is shown, else at the end.
  function insertText(item, text) {
    var m = monaco();
    var ed = editor();
    var position = isCurrent(item) ? ed.getPosition() : endPosition(item.model);
    var range = new m.Range(position.lineNumber, position.column, position.lineNumber, position.column);
    var edit = {range: range, text: text, forceMoveMarkers: true};
    if (isCurrent(item)) {
      var offset = item.model.getOffsetAt(position);
      ed.executeEdits('nicos', [edit]);
      var end = item.model.getPositionAt(offset + text.length);
      ed.setPosition(end);
      ed.revealPositionInCenterIfOutsideViewport(end);
    } else {
      item.model.pushEditOperations([], [edit], function () { return null; });
    }
  }

  function moveToEnd(item) {
    if (isCurrent(item)) {
      var position = endPosition(item.model);
      editor().setPosition(position);
      editor().revealPositionInCenterIfOutsideViewport(position);
    }
  }

  function setMarkers(item, markers) {
    var m = monaco();
    m.editor.setModelMarkers(item.model, 'nicos', markers.map(function (marker) {
      var line = marker.line || 1;
      return {
        severity: m.MarkerSeverity.Error,
        message: marker.message,
        startLineNumber: line,
        startColumn: marker.column || 1,
        endLineNumber: marker.endLine || line,
        endColumn: marker.endColumn ||
          item.model.getLineMaxColumn(Math.min(line, item.model.getLineCount())),
      };
    }));
  }

  function applyTheme(base, background) {
    var m = monaco();
    m.editor.defineTheme('nicos', {base: base, inherit: true, rules: [], colors: {'editor.background': background}});
    m.editor.setTheme('nicos');
  }

  // -- entry points for Python ------------------------------------------------

  nicos.handle = function (message) {
    var ed = editor();
    var item = entry(message.model);
    switch (message.cmd) {
      case 'providers':
        return installProviders();
      case 'theme':
        return applyTheme(message.base, message.background);
      case 'options':
        return updateOptions(message.options);
      case 'settings':
        nicos.comment = message.comment || '##';
        return applyComment();
      case 'names':
        nicos.names = message.names || {};
        return;
      case 'createModel':
        return createModel(message.model);
      case 'showModel':
        return showModel(message.model);
      case 'disposeModel':
        return disposeModel(message.model);
      case 'setText':
        return item && setText(item, message.text);
      case 'setModified':
        return item && setModified(item, message.modified);
      case 'setLanguage':
        return item && monaco().editor.setModelLanguage(item.model, message.language);
      case 'insert':
        return item && insertText(item, message.text);
      case 'moveToEnd':
        return item && moveToEnd(item);
      case 'undoStop':
        return item && item.model.pushStackElement();
      case 'markers':
        return item && setMarkers(item, message.markers);
      case 'focus':
        return ed.focus();
      case 'trigger':  // a command or action of the editor
        return ed.trigger('nicos', message.handler, null);
      default:
        console.warn('[nicos] unknown command ' + message.cmd);
    }
  };

  // The queries of Python return JSON: as a plain string, the text would lose
  // its byte order mark on the way.
  nicos.text = function (id) {
    var item = entry(id);
    return JSON.stringify(item ? item.model.getValue(undefined, true) : null);
  };

  nicos.isModified = function (id) {
    var item = entry(id);
    return JSON.stringify(item ? item.modified : null);
  };
})();
