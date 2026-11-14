// web-task-bot recorder. Injected into every frame of every page while
// recording. It watches what the person does and reports each action to
// Python through the `__wtb_record` binding, together with several
// alternative ways ("candidates") to find the same element again later.
(() => {
  if (window.__wtbInstalled) return;
  window.__wtbInstalled = true;

  const IS_TOP = window === window.top;
  const TESTID_ATTRS = ['data-testid', 'data-test-id', 'data-test', 'data-qa', 'data-cy', 'data-automation-id'];
  const TEXT_INPUT_TYPES = new Set(['', 'text', 'email', 'password', 'number', 'tel', 'url', 'search',
    'date', 'time', 'datetime-local', 'month', 'week', 'color']);
  const DATE_TYPES = new Set(['date', 'time', 'datetime-local', 'month', 'week']);
  const CLICKABLE = 'button, a, input, select, textarea, label, summary, option, ' +
    '[role=button], [role=link], [role=menuitem], [role=menuitemcheckbox], [role=menuitemradio], ' +
    '[role=option], [role=tab], [role=checkbox], [role=radio], [role=switch], [role=combobox], ' +
    '[role=treeitem], [role=gridcell], [onclick], [contenteditable=""], [contenteditable=true]';

  let mode = 'record';          // record | wait | assert | extract | paused
  let toolbarHost = null;

  // ------------------------------------------------------------------ utils
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const send = (payload) => {
    try {
      if (typeof window.__wtb_record === 'function') window.__wtb_record(payload);
    } catch (e) { /* page is navigating away */ }
  };
  const looksGenerated = (v) =>
    !v || v.length > 48 || /\d{4,}/.test(v) || /^[a-f0-9-]{12,}$/i.test(v) ||
    /^(:r|ember|react-|mui-|ext-gen|j_idt|headlessui|radix-|rc_|__)/i.test(v) || /[:]{1}r\w+:/.test(v);
  const cssUnique = (sel, root) => {
    try { return (root || document).querySelectorAll(sel).length === 1; } catch (e) { return false; }
  };
  const isVisible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none';
  };
  const inToolbar = (el) => toolbarHost && (el === toolbarHost || toolbarHost.contains(el));

  function implicitRole(el) {
    const explicit = (el.getAttribute('role') || '').split(/\s+/)[0];
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    switch (tag) {
      case 'a': return el.hasAttribute('href') ? 'link' : null;
      case 'button': return 'button';
      case 'select': return (el.multiple || el.size > 1) ? 'listbox' : 'combobox';
      case 'textarea': return 'textbox';
      case 'option': return 'option';
      case 'summary': return null;
      case 'h1': case 'h2': case 'h3': case 'h4': case 'h5': case 'h6': return 'heading';
      case 'input':
        if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
        if (type === 'checkbox') return 'checkbox';
        if (type === 'radio') return 'radio';
        if (type === 'range') return 'slider';
        if (type === 'number') return 'spinbutton';
        if (type === 'search') return 'searchbox';
        if (['', 'text', 'email', 'tel', 'url'].includes(type)) return 'textbox';
        return null;
    }
    return null;
  }

  function labelText(el) {
    if (el.labels && el.labels.length) return norm(el.labels[0].innerText);
    const wrap = el.closest && el.closest('label');
    if (wrap) return norm(wrap.innerText);
    return '';
  }

  function accessibleName(el) {
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\s+/).map((id) => document.getElementById(id))
        .filter(Boolean).map((n) => norm(n.innerText || n.textContent)).join(' ');
      if (t) return t;
    }
    const aria = norm(el.getAttribute('aria-label'));
    if (aria) return aria;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') {
      if (['submit', 'button', 'reset'].includes(type)) return norm(el.value);
      const lt = labelText(el);
      if (lt) return lt;
      return norm(el.getAttribute('title') || el.getAttribute('placeholder'));
    }
    const text = norm(el.innerText || el.textContent);
    if (text) return text;
    const img = el.querySelector && el.querySelector('img[alt]');
    if (img) return norm(img.getAttribute('alt'));
    return norm(el.getAttribute('title'));
  }

  function cssPath(el) {
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
      if (node.id && !looksGenerated(node.id)) {
        parts.unshift('#' + CSS.escape(node.id));
        const sel = parts.join(' > ');
        if (cssUnique(sel)) return sel;
      } else {
        let part = node.tagName.toLowerCase();
        const classes = [...node.classList].filter((c) => !looksGenerated(c) && !/^(is-|has-|active|focus|hover|ng-|css-|sc-)/.test(c)).slice(0, 2);
        if (classes.length) part += '.' + classes.map((c) => CSS.escape(c)).join('.');
        const parent = node.parentElement;
        if (parent) {
          const same = [...parent.children].filter((c) => c.tagName === node.tagName);
          if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
        }
        parts.unshift(part);
        const sel = parts.join(' > ');
        if (cssUnique(sel)) return sel;
      }
      node = node.parentElement;
    }
    return parts.join(' > ');
  }

  const ROLE_ES = {
    button: 'botón', link: 'enlace', textbox: 'campo', combobox: 'lista', listbox: 'lista',
    checkbox: 'casilla', radio: 'opción', option: 'opción', menuitem: 'menú', tab: 'pestaña',
    searchbox: 'buscador', spinbutton: 'campo numérico', heading: 'título', switch: 'interruptor',
  };

  function describe(el) {
    const candidates = [];
    const tag = el.tagName.toLowerCase();
    for (const attr of TESTID_ATTRS) {
      const v = el.getAttribute(attr);
      if (v && cssUnique(`[${attr}="${CSS.escape(v)}"]`)) { candidates.push({ testid: v, attr }); break; }
    }
    if (el.id && !looksGenerated(el.id) && cssUnique(`[id="${CSS.escape(el.id)}"]`)) candidates.push({ id: el.id });
    const role = implicitRole(el);
    const name = accessibleName(el);
    if (role && name && name.length <= 80) candidates.push({ role, name });
    if (['input', 'select', 'textarea'].includes(tag)) {
      const lt = labelText(el);
      if (lt && lt.length <= 80) candidates.push({ label: lt });
      const ph = norm(el.getAttribute('placeholder'));
      if (ph) candidates.push({ placeholder: ph });
      const nm = el.getAttribute('name');
      if (nm) {
        let sel = `${tag}[name="${CSS.escape(nm)}"]`;
        const type = (el.getAttribute('type') || '').toLowerCase();
        if ((type === 'radio' || type === 'checkbox') && el.value) sel += `[value="${CSS.escape(el.value)}"]`;
        if (cssUnique(sel)) candidates.push({ css: sel });
      }
    } else {
      const text = norm(el.innerText);
      if (text && text.length <= 60 && !candidates.some((c) => c.name === text)) candidates.push({ text });
    }
    if (role && !name) candidates.push({ role });
    const path = cssPath(el);
    if (path && !candidates.some((c) => c.css === path)) candidates.push({ css: path });

    const roleEs = ROLE_ES[role] || tag;
    const shown = name || norm(el.getAttribute('placeholder')) || (el.id ? '#' + el.id : '');
    return {
      candidates,
      desc: shown ? `${roleEs} "${shown.slice(0, 50)}"` : roleEs,
      visible: isVisible(el),
      frames: frameChain(),
    };
  }
  window.__wtbDescribe = (el) => { const d = describe(el); delete d.frames; return d; };

  // How to reach this frame from the top page, computed right now (the
  // iframe may be gone by the time Python processes the event). Only works
  // for same-origin frames; null tells Python to work it out itself.
  function frameChain() {
    const chain = [];
    let w = window;
    try {
      while (w !== w.top) {
        const fe = w.frameElement;
        const parentDescribe = w.parent.__wtbDescribe;
        if (!fe || !parentDescribe) return null;
        const d = parentDescribe(fe);
        chain.unshift({ desc: d.desc.replace(/^iframe/, 'marco'), find: d.candidates });
        w = w.parent;
      }
    } catch (e) {
      return null;
    }
    return chain;
  }

  const isTextInput = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'textarea') return true;
    if (tag === 'input') return TEXT_INPUT_TYPES.has((el.getAttribute('type') || '').toLowerCase());
    return el.isContentEditable;
  };
  const isPassword = (el) => el.tagName === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'password';
  const textOf = (el) => (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT')
    ? String(el.value || '') : norm(el.innerText || el.textContent);

  // ----------------------------------------------------------- text inputs
  // Typing fires an `input` event per keystroke; we debounce and only send
  // the final value, flushing early whenever anything else happens.
  let pending = null;
  function flushFill() {
    if (!pending) return;
    const { el } = pending;
    clearTimeout(pending.timer);
    pending = null;
    const d = describe(el);
    const value = el.isContentEditable ? norm(el.innerText) : el.value;
    send({ type: 'fill', target: d, value: isPassword(el) ? null : value, password: isPassword(el) });
  }
  function scheduleFill(el) {
    if (pending && pending.el !== el) flushFill();
    if (pending) clearTimeout(pending.timer);
    pending = { el, timer: setTimeout(flushFill, 400) };
  }

  document.addEventListener('input', (e) => {
    const el = e.target;
    if (!(el instanceof Element) || inToolbar(el) || mode === 'paused') return;
    if (!e.isTrusted) return;
    if (isTextInput(el)) scheduleFill(el);
  }, true);

  document.addEventListener('change', (e) => {
    const el = e.target;
    if (!(el instanceof Element) || inToolbar(el) || mode === 'paused') return;
    const tag = el.tagName.toLowerCase();
    if (tag === 'select') {
      flushFill();
      const d = describe(el);
      const opts = [...el.selectedOptions];
      send({
        type: 'select', target: d,
        values: opts.map((o) => o.value), labels: opts.map((o) => norm(o.label || o.text)),
        optional: !d.visible,
      });
    } else if (tag === 'input' && (el.getAttribute('type') || '').toLowerCase() === 'file') {
      flushFill();
      send({ type: 'upload', target: describe(el), files: [...(el.files || [])].map((f) => f.name) });
    } else if (isTextInput(el) && e.isTrusted) {
      if (pending && pending.el === el) flushFill();
    } else if (tag === 'input' && DATE_TYPES.has((el.getAttribute('type') || '').toLowerCase())) {
      // Native date pickers (and tools) may set the value programmatically.
      if (pending && pending.el !== el) flushFill();
      pending = { el, timer: null };
      flushFill();
    }
  }, true);

  document.addEventListener('focusout', (e) => {
    if (pending && pending.el === e.target) flushFill();
  }, true);

  // ------------------------------------------------------------------ keys
  document.addEventListener('keydown', (e) => {
    if (!e.isTrusted || mode === 'paused' || inToolbar(e.target)) return;
    if (e.key !== 'Enter' && e.key !== 'Escape') return;
    const el = e.target instanceof Element ? e.target : null;
    if (e.key === 'Enter' && el && el.tagName === 'TEXTAREA') return;   // newline, not submit
    flushFill();
    send({ type: 'press', key: e.key, target: el && el !== document.body ? describe(el) : null });
  }, true);

  // ---------------------------------------------------------------- clicks
  document.addEventListener('click', (e) => {
    if (!e.isTrusted || !(e.target instanceof Element) || inToolbar(e.target)) return;
    if (mode === 'paused') return;
    const raw = e.target;
    let el = raw.closest(CLICKABLE) || raw;

    if (mode === 'wait' || mode === 'assert' || mode === 'extract') {
      // Inspection modes: the click selects an element instead of acting.
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
      const target = raw.closest('input, select, textarea, button, a') || raw;
      const d = describe(target);
      const text = textOf(target);
      if (mode === 'wait') send({ type: 'wait_for', target: d });
      if (mode === 'assert') send({ type: 'assert_text', target: d, text });
      if (mode === 'extract') {
        const column = window.prompt('Nombre de la columna donde guardar este dato:', 'dato');
        if (column) send({ type: 'extract', target: d, column: norm(column), sample: text });
      }
      send({ type: 'mode_done' });
      return;
    }

    flushFill();
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();

    // A click on a <label> is forwarded by the browser to its control, which
    // produces its own click event: record only that one.
    if (tag === 'label' && el.control) return;
    if (tag === 'select' || tag === 'option') return;            // recorded on change
    if (tag === 'input' && type === 'file') return;               // recorded on change
    if (isTextInput(el) && !el.readOnly) return;                  // fill focuses it anyway

    if (tag === 'input' && (type === 'checkbox' || type === 'radio')) {
      send({ type: el.checked ? 'check' : 'uncheck', target: describe(el) });
      return;
    }
    send({ type: 'click', target: describe(el) });
  }, true);

  window.addEventListener('pagehide', flushFill, true);
  window.__wtbFlush = flushFill;

  // --------------------------------------------------------------- page load
  if (IS_TOP) {
    const announce = () => {
      const nav = performance.getEntriesByType('navigation')[0];
      send({ type: 'pageload', url: location.href, navType: nav ? nav.type : 'navigate' });
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', announce);
    else announce();
  }

  // ---------------------------------------------------------------- toolbar
  window.__wtbSetMode = (m) => {
    mode = m;
    document.documentElement.style.cursor = (m === 'record' || m === 'paused') ? '' : 'crosshair';
    if (window.__wtbRefreshToolbar) window.__wtbRefreshToolbar();
  };
  window.__wtbSetCount = (n) => {
    window.__wtbCount = n;
    if (window.__wtbRefreshToolbar) window.__wtbRefreshToolbar();
  };

  function buildToolbar() {
    if (!IS_TOP || toolbarHost || !document.body) return;
    toolbarHost = document.createElement('div');
    toolbarHost.setAttribute('data-wtb-toolbar', '');
    toolbarHost.style.cssText = 'position:fixed;z-index:2147483647;right:12px;bottom:12px;';
    const root = toolbarHost.attachShadow({ mode: 'open' });
    root.innerHTML = `
      <style>
        .bar{font:13px/1.2 system-ui,sans-serif;background:#111827;color:#f9fafb;border-radius:10px;
             padding:8px;box-shadow:0 6px 24px rgba(0,0,0,.35);display:flex;gap:6px;align-items:center}
        .dot{width:10px;height:10px;border-radius:50%;background:#ef4444;margin:0 4px}
        .paused .dot{background:#f59e0b}
        button{font:inherit;border:0;border-radius:6px;padding:6px 9px;cursor:pointer;background:#374151;color:#f9fafb}
        button:hover{background:#4b5563}
        button.on{background:#2563eb}
        button.stop{background:#dc2626}
        .msg{max-width:220px;opacity:.85}
      </style>
      <div class="bar">
        <span class="dot"></span><span class="msg">Grabando</span>
        <button data-m="wait" title="El siguiente clic marca un elemento que hay que esperar">Esperar</button>
        <button data-m="assert" title="El siguiente clic marca un texto que debe aparecer">Verificar</button>
        <button data-m="extract" title="El siguiente clic guarda un dato de la página en los resultados">Extraer</button>
        <button data-m="paused" title="Pausa la grabación">Pausar</button>
        <button class="stop" title="Termina y guarda el bot">Terminar</button>
      </div>`;
    const bar = root.querySelector('.bar');
    const msg = root.querySelector('.msg');
    window.__wtbRefreshToolbar = () => {
      const n = window.__wtbCount || 0;
      bar.classList.toggle('paused', mode === 'paused');
      root.querySelectorAll('button[data-m]').forEach((b) => b.classList.toggle('on', b.dataset.m === mode));
      msg.textContent = {
        record: `Grabando · ${n} pasos`, paused: 'En pausa',
        wait: 'Haz clic en lo que hay que esperar', assert: 'Haz clic en el texto a verificar',
        extract: 'Haz clic en el dato a extraer',
      }[mode] || '';
    };
    root.querySelectorAll('button[data-m]').forEach((b) => b.addEventListener('click', (e) => {
      e.stopPropagation();
      flushFill();
      send({ type: 'set_mode', mode: mode === b.dataset.m ? 'record' : b.dataset.m });
    }));
    root.querySelector('.stop').addEventListener('click', (e) => {
      e.stopPropagation();
      flushFill();
      send({ type: 'stop' });
    });
    document.body.appendChild(toolbarHost);
    window.__wtbRefreshToolbar();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', buildToolbar);
  else buildToolbar();
})();
