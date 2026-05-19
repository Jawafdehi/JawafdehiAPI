(function () {
  "use strict";

  const TURNDOWN_CDN =
    "https://unpkg.com/turndown@7.2.4/dist/turndown.js";

  function loadScript(src, callback) {
    const script = document.createElement("script");
    script.src = src;
    script.onload = callback;
    script.onerror = function () {
      console.warn(
        "[easymde_clipboard] Failed to load Turndown. HTML-to-Markdown paste conversion disabled."
      );
    };
    document.head.appendChild(script);
  }

  function initClipboardHandler() {
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

        turndownService.remove(["style", "script", "meta", "link"]);

        const markdown = turndownService.turndown(html);

        const cm = cmElement.CodeMirror;
        if (cm) {
          cm.replaceSelection(markdown);
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
