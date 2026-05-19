(function () {
  "use strict";

  const TURNDOWN_CDN =
    "https://unpkg.com/turndown@7.2.4/dist/turndown.js";

  function loadScript(src, callback) {
    const script = document.createElement("script");
    script.src = src;
    script.onload = function () {
      console.debug("[easymde_clipboard] Loaded: " + src);
      callback();
    };
    script.onerror = function () {
      console.warn(
        "[easymde_clipboard] Failed to load Turndown from CDN. HTML-to-Markdown paste conversion disabled."
      );
      if (callback) callback();
    };
    document.head.appendChild(script);
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

          turndownService.remove(["style", "script", "meta", "link"]);

          const markdown = turndownService.turndown(html);

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
    loadScript(TURNDOWN_CDN, initClipboardHandler);
  } else {
    initClipboardHandler();
  }
})();
