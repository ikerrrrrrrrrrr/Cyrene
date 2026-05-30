// Inject action buttons (copy, edit) into code blocks rendered in chat messages.
// Uses MutationObserver to handle dynamically loaded messages.

(function () {
  if (typeof document === "undefined") return;

  function createButton(text, title, onClick) {
    var btn = document.createElement("button");
    btn.className = "code-action-btn";
    btn.textContent = text;
    btn.title = title;
    btn.type = "button";
    btn.addEventListener("click", onClick);
    return btn;
  }

  function getCodeText(pre) {
    var code = pre.querySelector("code");
    if (!code) return "";
    // Extract raw text, stripping line-number spans.
    var clone = code.cloneNode(true);
    var nums = clone.querySelectorAll(".hljs-ln-n");
    for (var i = 0; i < nums.length; i++) nums[i].remove();
    return clone.textContent || "";
  }

  function getLanguage(pre) {
    var code = pre.querySelector("code");
    if (code && code.dataset.language) return code.dataset.language;
    var cls = pre.className || "";
    var m = cls.match(/language-(\S+)/);
    return m ? m[1] : "";
  }

  function addActions(pre) {
    if (pre.dataset.actionsAdded === "1") return;
    pre.dataset.actionsAdded = "1";

    var code = getCodeText(pre);
    var lang = getLanguage(pre);

    var bar = document.createElement("div");
    bar.className = "code-block-actions";

    if (lang) {
      var label = document.createElement("span");
      label.className = "code-lang-label";
      label.textContent =
        (window.CodeHighlight && window.CodeHighlight.getLanguageName(lang)) || lang;
      bar.appendChild(label);
    }

    var spacer = document.createElement("span");
    spacer.style.flex = "1";
    bar.appendChild(spacer);

    bar.appendChild(
      createButton("Copy", "Copy code", function () {
        navigator.clipboard.writeText(code).then(
          function () {
            this.textContent = "Copied!";
            var self = this;
            setTimeout(function () {
              self.textContent = "Copy";
            }, 1500);
          }.bind(this),
          function () {
            this.textContent = "Failed";
          }.bind(this)
        );
      })
    );

    bar.appendChild(
      createButton("Edit", "Open in editor", function () {
        var evt = new CustomEvent("cyrene:open-editor", {
          detail: { code: code, language: lang },
          bubbles: true,
        });
        window.dispatchEvent(evt);
      })
    );

    pre.style.position = "relative";
    pre.appendChild(bar);
  }

  function scanMessages(root) {
    var pres = root.querySelectorAll
      ? root.querySelectorAll("pre")
      : [];
    for (var i = 0; i < pres.length; i++) addActions(pres[i]);
    // Also scan the root itself if it's a pre.
    if (root.tagName === "PRE") addActions(root);
  }

  var _retryCount = 0;
  var _maxRetries = 100;

  function start() {
    var container = document.querySelector(".msg-list");
    if (!container) {
      _retryCount++;
      if (_retryCount <= _maxRetries) {
        setTimeout(start, 300);
      }
      return;
    }
    scanMessages(container);
    var observer = new MutationObserver(function (mutations) {
      for (var i = 0; i < mutations.length; i++) {
        var added = mutations[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          if (added[j].nodeType === 1) scanMessages(added[j]);
        }
      }
    });
    observer.observe(container, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
