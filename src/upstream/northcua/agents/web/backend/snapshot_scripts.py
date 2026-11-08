"""JavaScript snippets evaluated by the Playwright web backend."""

from __future__ import annotations

SNAPSHOT_SCRIPT = r"""
({ attrName, depth, interactiveOnly, startIndex, maxRefs, maxVisited, maxVisibleTextChars }) => {
  const maxDepth = Number.isFinite(depth) ? Math.max(0, depth) : 6;
  const refLimit = Number.isFinite(maxRefs) ? Math.max(1, maxRefs) : Infinity;
  const visitLimit = Number.isFinite(maxVisited) ? Math.max(1, maxVisited) : Infinity;
  const candidates = [
    "a[href]",
    "button",
    "input",
    "textarea",
    "select",
    "summary",
    "label",
    "[role]",
    "[tabindex]",
    "[contenteditable='']",
    "[contenteditable='true']"
  ].join(",");
  const textBlocks = "h1,h2,h3,h4,h5,h6,p,li,td,th";

  const childrenOf = (node) => {
    const children = [];
    if (node && node.children) {
      children.push(...Array.from(node.children));
    }
    if (node && node.shadowRoot && node.shadowRoot.children) {
      children.push(...Array.from(node.shadowRoot.children));
    }
    return children;
  };

  const isVisible = (el) => {
    const style = window.getComputedStyle(el);
    if (!style || style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const trim = (value, limit = 160) => {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
  };
  const boundedText = (value, limit) => {
    const maxChars = Number.isFinite(limit) ? Math.max(0, limit) : 0;
    const text = String(value || "").replace(/\s+/g, " ").trim();
    if (maxChars <= 0) return { text: "", chars: text.length, truncated: text.length > 0 };
    return {
      text: text.length > maxChars ? `${text.slice(0, maxChars - 1)}…` : text,
      chars: text.length,
      truncated: text.length > maxChars
    };
  };

  const explicitRole = (el) => trim(el.getAttribute("role"), 48).toLowerCase();
  const inferredRole = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = trim(el.getAttribute("type"), 32).toLowerCase();
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "textarea") return "textbox";
    if (tag === "select") return "combobox";
    if (tag === "summary") return "button";
    if (tag === "input") {
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "range") return "slider";
      if (type === "number") return "spinbutton";
      if (type === "search") return "searchbox";
      return "textbox";
    }
    return "";
  };

  const roleOf = (el) => explicitRole(el) || inferredRole(el);
  const labelTextOf = (el, limit = 160) => {
    const labels = el.labels ? Array.from(el.labels) : [];
    if (labels.length) {
      const text = trim(labels.map((label) => label.textContent || "").join(" "), limit);
      if (text) return text;
    }
    if (el.closest) {
      const label = el.closest("label");
      if (label) {
        const text = trim(label.textContent || "", limit);
        if (text) return text;
      }
    }
    return "";
  };
  const nameOf = (el) => {
    const tag = el.tagName.toLowerCase();
    const aria = trim(el.getAttribute("aria-label"));
    if (aria) return aria;
    const labelledBy = trim(el.getAttribute("aria-labelledby"));
    if (labelledBy) {
      const parts = labelledBy.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean);
      const text = trim(parts.map((node) => node.textContent || "").join(" "));
      if (text) return text;
    }
    const title = trim(el.getAttribute("title"));
    if (title) return title;
    const placeholder = trim(el.getAttribute("placeholder"));
    if (placeholder) return placeholder;
    if (tag === "input" || tag === "textarea" || tag === "select") {
      const label = labelTextOf(el);
      if (label) return label;
    }
    if (tag === "input") {
      const value = trim(el.getAttribute("value") || el.value, 120);
      if (value) return value;
    }
    return trim(el.textContent || "", 160);
  };

  const attrsOf = (el) => {
    const out = {};
    for (const name of ["type", "href", "aria-disabled", "disabled", "aria-hidden", "hidden", "placeholder", "title"]) {
      if (el.hasAttribute(name)) {
        out[name] = el.getAttribute(name);
      }
    }
    const tag = el.tagName.toLowerCase();
    const type = trim(el.getAttribute("type"), 32).toLowerCase();
    if ((tag === "input" || tag === "textarea" || tag === "select") && type !== "password") {
      const value = trim(el.value || "", 160);
      if (value) out.value = value;
    }
    if (tag === "input" && (type === "checkbox" || type === "radio")) {
      out.checked = Boolean(el.checked);
    }
    return out;
  };

  const textOf = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = trim(el.getAttribute("type"), 32).toLowerCase();
    if ((tag === "input" || tag === "textarea" || tag === "select") && type !== "password") {
      return trim(el.value || "", 160);
    }
    return trim(el.textContent || "", 160);
  };

  const isCandidate = (el) => el.matches(candidates);
  const isTextBlock = (el) => !interactiveOnly && el.matches(textBlocks);

  const refs = {};
  const lines = [];
  let index = Number.isFinite(startIndex) ? Math.max(1, startIndex) : 1;

  let visited = 0;
  let truncated = false;
  let refCount = 0;

  const walk = (node, currentDepth) => {
    if (visited++ >= visitLimit || refCount >= refLimit) {
      truncated = true;
      return;
    }
    for (const child of childrenOf(node)) {
      if (visited >= visitLimit || refCount >= refLimit) {
        truncated = true;
        return;
      }
      if (!(child instanceof Element)) continue;
      child.removeAttribute(attrName);
      const candidate = isCandidate(child);
      const include = isVisible(child) && (candidate || (currentDepth <= maxDepth && isTextBlock(child)));
      if (include) {
        const ref = `e${index++}`;
        const role = roleOf(child) || child.tagName.toLowerCase();
        const name = nameOf(child);
        const text = textOf(child);
        child.setAttribute(attrName, ref);
        refs[ref] = {
          role,
          name,
          text,
          tag: child.tagName.toLowerCase(),
          attributes: attrsOf(child)
        };
        const label = name || text || child.tagName.toLowerCase();
        lines.push(`${role} "${label}" [ref=@${ref}]`);
        refCount += 1;
      }
      walk(child, currentDepth + 1);
    }
  };

  walk(document.documentElement, 0);
  const visibleText = boundedText(document.body ? document.body.innerText : "", maxVisibleTextChars);
  return {
    url: window.location.href,
    title: document.title || "",
    origin: window.location.origin || "",
    snapshot: lines.join("\n"),
    refs,
    nextRefIndex: index,
    refCount,
    visited,
    maxRefs: Number.isFinite(refLimit) ? refLimit : null,
    maxVisited: Number.isFinite(visitLimit) ? visitLimit : null,
    truncated,
    visibleText: visibleText.text,
    visibleTextChars: visibleText.chars,
    visibleTextTruncated: visibleText.truncated
  };
}
"""

FAST_SNAPSHOT_SCRIPT = r"""
({ attrName, startIndex, maxRefs, maxVisibleTextChars }) => {
  const refLimit = Number.isFinite(maxRefs) ? Math.max(1, maxRefs) : 128;
  const candidates = [
    "a[href]",
    "button",
    "input",
    "textarea",
    "select",
    "summary",
    "label",
    "[role]",
    "[tabindex]",
    "[contenteditable='']",
    "[contenteditable='true']"
  ].join(",");

  const trim = (value, limit = 120) => {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
  };
  const boundedText = (value, limit) => {
    const maxChars = Number.isFinite(limit) ? Math.max(0, limit) : 0;
    const text = String(value || "").replace(/\s+/g, " ").trim();
    if (maxChars <= 0) return { text: "", chars: text.length, truncated: text.length > 0 };
    return {
      text: text.length > maxChars ? `${text.slice(0, maxChars - 1)}…` : text,
      chars: text.length,
      truncated: text.length > maxChars
    };
  };

  const roleOf = (el) => {
    const explicit = trim(el.getAttribute("role"), 48).toLowerCase();
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    const type = trim(el.getAttribute("type"), 32).toLowerCase();
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "textarea") return "textbox";
    if (tag === "select") return "combobox";
    if (tag === "summary") return "button";
    if (tag === "input") {
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "range") return "slider";
      if (type === "number") return "spinbutton";
      if (type === "search") return "searchbox";
      return "textbox";
    }
    return tag;
  };

  const labelTextOf = (el, limit = 120) => {
    const labels = el.labels ? Array.from(el.labels) : [];
    if (labels.length) {
      const text = trim(labels.map((label) => label.textContent || "").join(" "), limit);
      if (text) return text;
    }
    if (el.closest) {
      const label = el.closest("label");
      if (label) {
        const text = trim(label.textContent || "", limit);
        if (text) return text;
      }
    }
    return "";
  };

  const nameOf = (el) => {
    const tag = el.tagName.toLowerCase();
    return (
      trim(el.getAttribute("aria-label")) ||
      trim(el.getAttribute("title")) ||
      trim(el.getAttribute("placeholder")) ||
      ((tag === "input" || tag === "textarea" || tag === "select") ? labelTextOf(el) : "") ||
      trim(el.getAttribute("value") || el.value, 80) ||
      trim(el.textContent || "", 120)
    );
  };

  const attrsOf = (el) => {
    const out = {};
    for (const name of ["type", "href", "aria-disabled", "disabled", "aria-hidden", "hidden", "placeholder", "title"]) {
      if (el.hasAttribute(name)) out[name] = el.getAttribute(name);
    }
    const tag = el.tagName.toLowerCase();
    const type = trim(el.getAttribute("type"), 32).toLowerCase();
    if ((tag === "input" || tag === "textarea" || tag === "select") && type !== "password") {
      const value = trim(el.value || "", 120);
      if (value) out.value = value;
    }
    if (tag === "input" && (type === "checkbox" || type === "radio")) {
      out.checked = Boolean(el.checked);
    }
    return out;
  };

  const textOf = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = trim(el.getAttribute("type"), 32).toLowerCase();
    if ((tag === "input" || tag === "textarea" || tag === "select") && type !== "password") {
      return trim(el.value || "", 120);
    }
    return trim(el.textContent || "", 120);
  };

  const isVisible = (el, rect) => {
    const style = window.getComputedStyle(el);
    if (!style || style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) {
      return false;
    }
    return rect.width > 0 && rect.height > 0;
  };

  const refs = {};
  const lines = [];
  let index = Number.isFinite(startIndex) ? Math.max(1, startIndex) : 1;
  let visited = 0;
  for (const el of Array.from(document.querySelectorAll(candidates))) {
    visited += 1;
    if (Object.keys(refs).length >= refLimit) break;
    el.removeAttribute(attrName);
    const rect = el.getBoundingClientRect();
    if (!isVisible(el, rect)) continue;
    const ref = `e${index++}`;
    const role = roleOf(el);
    const name = nameOf(el);
    const text = textOf(el);
    el.setAttribute(attrName, ref);
    refs[ref] = { role, name, text, tag: el.tagName.toLowerCase(), attributes: attrsOf(el) };
    const label = name || text || el.tagName.toLowerCase();
    lines.push(`${role} "${label}" [ref=@${ref}]`);
  }
  const visibleText = boundedText(document.body ? document.body.innerText : "", maxVisibleTextChars);
  return {
    url: window.location.href,
    title: document.title || "",
    origin: window.location.origin || "",
    snapshot: lines.join("\n"),
    refs,
    nextRefIndex: index,
    refCount: Object.keys(refs).length,
    visited,
    maxRefs: refLimit,
    maxVisited: visited,
    truncated: Object.keys(refs).length >= refLimit,
    visibleText: visibleText.text,
    visibleTextChars: visibleText.chars,
    visibleTextTruncated: visibleText.truncated,
    fallback: "bounded_fast_query"
  };
}
"""

REF_PROBE_SCRIPT = r"""
({ attrName, refs }) => {
  const childrenOf = (node) => {
    const children = [];
    if (node && node.children) {
      children.push(...Array.from(node.children));
    }
    if (node && node.shadowRoot && node.shadowRoot.children) {
      children.push(...Array.from(node.shadowRoot.children));
    }
    return children;
  };

  const findByRef = (root, ref) => {
    const stack = [root.documentElement || root];
    while (stack.length) {
      const node = stack.pop();
      if (!node) continue;
      if (node instanceof Element && node.getAttribute(attrName) === ref) {
        return node;
      }
      for (const child of childrenOf(node)) {
        stack.push(child);
      }
    }
    return null;
  };

  const isVisible = (el, rect) => {
    const style = window.getComputedStyle(el);
    if (!style || style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) {
      return false;
    }
    return rect.width > 0 && rect.height > 0;
  };

  const isEnabled = (el) => {
    if (el.disabled === true) return false;
    if (String(el.getAttribute("aria-disabled") || "").toLowerCase() === "true") return false;
    return true;
  };

  const boxes = {};
  const states = {};
  const errors = {};
  for (const rawRef of refs || []) {
    const refId = String(rawRef || "").replace(/^@/, "");
    const modelRef = `@${refId}`;
    const el = findByRef(document, refId);
    if (!el) {
      errors[modelRef] = "stale ref; element not found in current page";
      continue;
    }
    const rect = el.getBoundingClientRect();
    boxes[modelRef] = {
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height
    };
    states[modelRef] = {
      visible: isVisible(el, rect),
      enabled: isEnabled(el)
    };
  }
  return { boxes, states, errors };
}
"""

__all__ = ["FAST_SNAPSHOT_SCRIPT", "REF_PROBE_SCRIPT", "SNAPSHOT_SCRIPT"]
