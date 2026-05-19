(function () {
  "use strict";

  const TURNDOWN_CDN =
    "https://unpkg.com/turndown@7.2.4/dist/turndown.js";
  const TURNDOWN_GFM_CDN =
    "https://unpkg.com/turndown-plugin-gfm@1.0.2/dist/turndown-plugin-gfm.js";

  let gfmScriptLoaded = false;
  let initDone = false;

  function loadScript(src, callback) {
    const script = document.createElement("script");
    script.src = src;
    script.onload = callback;
    script.onerror = function () {
      console.warn(
        "[easymde_clipboard] Failed to load script: " + src
      );
    };
    document.head.appendChild(script);
  }

  function ensureGfm(callback) {
    if (typeof turndownPluginGfm !== "undefined" && turndownPluginGfm.gfm) {
      callback();
      return;
    }
    if (gfmScriptLoaded) {
      let retries = 0;
      const check = setInterval(function () {
        if (typeof turndownPluginGfm !== "undefined" && turndownPluginGfm.gfm) {
          clearInterval(check);
          callback();
        } else if (++retries > 20) {
          clearInterval(check);
          console.warn("[easymde_clipboard] turndown-plugin-gfm not available, table conversion disabled.");
          callback();
        }
      }, 100);
      return;
    }
    gfmScriptLoaded = true;
    loadScript(TURNDOWN_GFM_CDN, function () {
      callback();
    });
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
      el.parentNode.replaceChild(replacement, el);
    }

    function walkAndNormalize(node) {
      const children = Array.prototype.slice.call(node.childNodes);
      for (let i = 0; i < children.length; i++) {
        const child = children[i];
        if (child.nodeType === Node.ELEMENT_NODE) {
          walkAndNormalize(child);
        }
      }

      if (node.nodeType === Node.ELEMENT_NODE && node.tagName === "SPAN") {
        const style = (node.getAttribute("style") || "").toLowerCase();
        const fontWeight = style.match(/font-weight\s*:\s*(\w+)/);
        if (fontWeight) {
          const weight = fontWeight[1];
          if (weight === "bold" || weight === "bolder" || weight === "700" || weight === "800" || weight === "900") {
            replaceWithTag(node, "strong");
            return;
          }
        }
        const fontStyle = style.match(/font-style\s*:\s*(\w+)/);
        if (fontStyle && fontStyle[1] === "italic") {
          replaceWithTag(node, "em");
          return;
        }
      }

      if (node.nodeType === Node.ELEMENT_NODE) {
        const cls = node.className || "";
        if (typeof cls === "string") {
          node.className = cls.split(/\s+/).filter(function (c) {
            return !/^[Mm]so/i.test(c);
          }).join(" ");
        }

        if (node.hasAttribute("style")) {
          let s = node.getAttribute("style");
          s = s.replace(/mso-[^:;]+:[^;]*;?/gi, "");
          s = s.replace(/-ms-[^:;]+:[^;]*;?/gi, "");
          s = s.trim();
          if (s) {
            node.setAttribute("style", s);
          } else {
            node.removeAttribute("style");
          }
        }

        const attrsToRemove = [];
        for (let j = 0; j < node.attributes.length; j++) {
          const attr = node.attributes[j];
          if (/^xmlns|^o:|^w:|^v:|^mso/i.test(attr.name)) {
            attrsToRemove.push(attr.name);
          }
        }
        for (let k = 0; k < attrsToRemove.length; k++) {
          node.removeAttribute(attrsToRemove[k]);
        }
      }
    }

    walkAndNormalize(root);

    return root.innerHTML;
  }

  function initClipboardHandler() {
    if (initDone) return;
    initDone = true;

    document.addEventListener(
      "paste",
      function (event) {
        const cmElement = event.target.closest(".CodeMirror");
        if (!cmElement) return;

        if (!cmElement.closest(".EasyMDEContainer")) return;

        const clipboardData = event.clipboardData;
        if (!clipboardData) return;

        if (clipboardData.files && clipboardData.files.length > 0) return;

        const html = clipboardData.getData("text/html");
        if (!html || html.trim().length === 0) return;

        if (typeof TurndownService === "undefined") return;

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
      },
      true
    );
  }

  if (typeof TurndownService === "undefined") {
    loadScript(TURNDOWN_CDN, function () {
      ensureGfm(initClipboardHandler);
    });
  } else {
    ensureGfm(initClipboardHandler);
  }
})();
