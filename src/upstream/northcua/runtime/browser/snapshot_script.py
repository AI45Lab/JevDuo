"""JavaScript source used by Playwright browser snapshots."""

from __future__ import annotations

BROWSER_SNAPSHOT_SCRIPT = """
({ selector, depth }) => {
  const root = selector ? document.querySelector(selector) : document.body;
  const maxDepth = typeof depth === 'number' ? depth : null;
  const elements = [];
  if (!root) {
    return { title: document.title || '', url: location.href || '', elements };
  }

  const roleMap = {
    a: 'link',
    button: 'button',
    select: 'combobox',
    textarea: 'textbox',
    summary: 'button',
    option: 'option',
    img: 'img',
    input: (el) => {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'submit' || type === 'button' || type === 'reset') return 'button';
      return 'textbox';
    },
    h1: 'heading',
    h2: 'heading',
    h3: 'heading',
    h4: 'heading',
    h5: 'heading',
    h6: 'heading'
  };

  const cssEscape = (value) => {
    if (window.CSS && typeof window.CSS.escape === 'function')
      return window.CSS.escape(value);
    return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\\\$&');
  };

  const inferRole = (el) => {
    const explicitRole = el.getAttribute('role');
    if (explicitRole)
      return explicitRole.trim();
    const tag = el.tagName.toLowerCase();
    const roleEntry = roleMap[tag];
    return typeof roleEntry === 'function' ? roleEntry(el) : (roleEntry || '');
  };

  const inferName = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria)
      return aria.trim();
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const pieces = labelledBy.split(/\\s+/).map((id) => document.getElementById(id)).filter(Boolean).map((node) => (node.innerText || node.textContent || '').trim()).filter(Boolean);
      if (pieces.length)
        return pieces.join(' ');
    }
    if (el instanceof HTMLInputElement) {
      const value = el.value || el.placeholder || '';
      if (value.trim())
        return value.trim();
    }
    const text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
    if (text)
      return text.slice(0, 200);
    return '';
  };

  const buildSelector = (el) => {
    if (el.id)
      return '#' + cssEscape(el.id);
    const tag = el.tagName.toLowerCase();
    const parts = [tag];
    const classes = Array.from(el.classList || []).slice(0, 3);
    if (classes.length)
      parts.push(...classes.map((cls) => '.' + cssEscape(cls)));
    return parts.join('');
  };

  const visit = (el, depthValue) => {
    if (!(el instanceof Element))
      return;
    if (maxDepth !== null && depthValue > maxDepth)
      return;
    const selector = buildSelector(el);
    elements.push({
      selector,
      role: inferRole(el),
      name: inferName(el),
      tag_name: el.tagName.toLowerCase(),
      depth: depthValue
    });
    for (const child of Array.from(el.children))
      visit(child, depthValue + 1);
  };

  visit(root, 0);
  return {
    title: document.title || '',
    url: location.href || '',
    elements
  };
}
"""
