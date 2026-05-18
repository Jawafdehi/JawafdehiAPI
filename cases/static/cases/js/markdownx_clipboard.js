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
        "[markdownx_clipboard] Failed to load Turndown. HTML-to-Markdown paste conversion disabled."
      );
    };
    document.head.appendChild(script);
  }

  function initClipboardHandler() {
    document.addEventListener("paste", function (event) {
      const target = event.target;
      if (!target.classList.contains("markdownx-editor")) return;

      const clipboardData = event.clipboardData;
      if (!clipboardData) return;

      if (clipboardData.files && clipboardData.files.length > 0) return;

      const html = clipboardData.getData("text/html");
      if (!html || html.trim().length === 0) return;

      if (typeof TurndownService === "undefined") return;

      event.preventDefault();

      const turndownService = new TurndownService({
        headingStyle: "atx",
        hr: "---",
        bulletListMarker: "-",
        codeBlockStyle: "fenced",
        emDelimiter: "*",
      });

      // Remove Word-specific cruft while preserving text
      turndownService.remove(["style", "script", "meta", "link"]);

      const markdown = turndownService.turndown(html);

      const start = target.selectionStart;
      const end = target.selectionEnd;
      const text = target.value;

      target.value =
        text.substring(0, start) + markdown + text.substring(end);
      target.selectionStart = target.selectionEnd =
        start + markdown.length;

      target.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  if (typeof TurndownService === "undefined") {
    loadScript(TURNDOWN_CDN, initClipboardHandler);
  } else {
    initClipboardHandler();
  }
})();
