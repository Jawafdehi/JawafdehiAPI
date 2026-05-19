(function () {
  "use strict";

  const TURNDOWN_CDN =
    "https://unpkg.com/turndown@7.2.4/dist/turndown.js";
  const TURNDOWN_GFM_CDN =
    "https://unpkg.com/turndown-plugin-gfm@1.0.2/dist/turndown-plugin-gfm.js";

  function loadScript(src, callback) {
    const script = document.createElement("script");
    script.src = src;
    script.onload = function () {
      console.debug("[easymde_clipboard] Loaded: " + src);
      callback();
    };
    script.onerror = function () {
      console.warn("[easymde_clipboard] Failed to load: " + src);
      if (callback) callback();
    };
    document.head.appendChild(script);
  }

  function normalizeWordHtml(html) {
    const doc = document.implementation.createHTMLDocument("");
    const root = doc.createElement("div");
    root.innerHTML = html;

    function replaceWithTag(el, tagName) {
      const replacement = doc.createElement(tagName);
      while (el.firstChild) {
        replacement.appendChild(el.firstChild);
      }
      if (el.parentNode) {
        el.parentNode.replaceChild(replacement, el);
      }
    }

    function walk(node) {
      const children = Array.prototype.slice.call(node.childNodes);
      for (let i = 0; i < children.length; i++) {
        if (children[i].nodeType === Node.ELEMENT_NODE) {
          walk(children[i]);
        }
      }

      if (node.nodeType !== Node.ELEMENT_NODE) return;

      // Normalize CSS-based formatting inside <span> tags
      if (node.tagName === "SPAN") {
        const style = (node.getAttribute("style") || "").toLowerCase();
        const fw = style.match(/font-weight\s*:\s*(\d+|bold|bolder)/);
        if (fw) {
          const w = fw[1];
          if (w === "bold" || w === "bolder" || parseInt(w) >= 600) {
            replaceWithTag(node, "strong");
            return;
          }
        }
        if (/font-style\s*:\s*italic/.test(style)) {
          replaceWithTag(node, "em");
          return;
        }
        if (/text-decoration\s*:\s*line-through/.test(style)) {
          replaceWithTag(node, "del");
          return;
        }
      }

      // Strip MSO classes
      const cls = node.className || "";
      if (typeof cls === "string" && /[Mm]so/i.test(cls)) {
        node.className = cls.split(/\s+/).filter(function (c) {
          return !/^[Mm]so/i.test(c);
        }).join(" ");
      }

      // Strip MSO styles and -ms- styles
      if (node.hasAttribute("style")) {
        let s = node.getAttribute("style");
        s = s.replace(/mso-[^:;]+:[^;]*;?/gi, "");
        s = s.replace(/-ms-[^:;]+:[^;]*;?/gi, "");
        s = s.trim();
        if (s) node.setAttribute("style", s);
        else node.removeAttribute("style");
      }

      // Remove MSO/Word XML namespace attributes
      const toRemove = [];
      for (let j = 0; j < node.attributes.length; j++) {
        if (/^(xmlns|o:|w:|v:|mso)/i.test(node.attributes[j].name)) {
          toRemove.push(node.attributes[j].name);
        }
      }
      for (let k = 0; k < toRemove.length; k++) {
        node.removeAttribute(toRemove[k]);
      }
    }

    walk(root);
    return root.innerHTML;
  }

  function initClipboardHandler() {
    console.debug("[easymde_clipboard] Paste handler registered.");

    document.addEventListener(
      "paste",
      function (event) {
        console.debug("[easymde_clipboard] paste event on", event.target.tagName,
          "| .CodeMirror:", !!event.target.closest(".CodeMirror"),
          "| html:", !!(event.clipboardData && event.clipboardData.getData("text/html")),
          "| target:", event.target);

        const cmElement = event.target.closest(".CodeMirror");
        if (!cmElement) return;

        if (!cmElement.closest(".EasyMDEContainer")) return;

        const clipboardData = event.clipboardData;
        if (!clipboardData) return;

        if (clipboardData.files && clipboardData.files.length > 0) return;

        const html = clipboardData.getData("text/html");
        if (!html || html.trim().length === 0) return;

        if (typeof TurndownService === "undefined") return;

        console.debug("[easymde_clipboard] Paste intercepted, converting HTML to Markdown...");

        try {
          event.preventDefault();
          event.stopPropagation();

          const turndownService = new TurndownService({
            headingStyle: "atx",
            hr: "---",
            bulletListMarker: "-",
            codeBlockStyle: "fenced",
            emDelimiter: "*",
          });

          if (typeof turndownPluginGfm !== "undefined" && turndownPluginGfm.gfm) {
            turndownService.use(turndownPluginGfm.gfm);
          }

          turndownService.remove(["style", "script", "meta", "link"]);

          const cleanHtml = normalizeWordHtml(html);
          const markdown = turndownService.turndown(cleanHtml);

          const cm = cmElement.CodeMirror;
          if (cm) {
            cm.replaceSelection(markdown);
          }
        } catch (e) {
          console.warn("[easymde_clipboard] Conversion failed, falling back to plain text:", e);
        }
      },
      true
    );
  }

  if (typeof TurndownService === "undefined") {
    loadScript(TURNDOWN_CDN, function () {
      loadScript(TURNDOWN_GFM_CDN, initClipboardHandler);
    });
  } else {
    loadScript(TURNDOWN_GFM_CDN, initClipboardHandler);
  }
})();
