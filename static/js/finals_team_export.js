(function () {
  var THEME_BG = {
    multi: "#120810",
    blue: "#0a1628",
    red: "#1a080c",
    yellow: "#1f1908",
  };

  function sanitizeFilename(name) {
    var s = (name || "team").replace(/[\\/:*?"<>|]+/g, "_").trim();
    return s.slice(0, 88) || "team";
  }

  function downloadDataUrl(filename, dataUrl) {
    var a = document.createElement("a");
    a.href = dataUrl;
    a.download = filename;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }

  /** html2canvas often renders input text blank or shifted — swap to a styled div in the clone only. */
  function replaceTeamTitleInClone(doc, originalRoot) {
    var cloneRoot = doc.getElementById(originalRoot.id);
    if (!cloneRoot) return;
    var origInp = originalRoot.querySelector(".final-team-name-input");
    var cloneInp = cloneRoot.querySelector(".final-team-name-input");
    if (!origInp || !cloneInp || !cloneInp.parentNode) return;
    var div = doc.createElement("div");
    div.className = "final-team-name-static";
    var text = (origInp.value || "").trim();
    if (!text) text = origInp.getAttribute("data-initial-name") || "";
    div.textContent = text;
    cloneInp.parentNode.replaceChild(div, cloneInp);
  }

  function bindSave() {
    document.querySelectorAll("[data-save-team-image]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var id = btn.getAttribute("data-save-team-image");
        var root = id ? document.getElementById(id) : null;
        if (!root || typeof html2canvas !== "function") {
          if (typeof html2canvas !== "function") {
            window.alert("Image export is still loading. Try again in a moment.");
          }
          return;
        }

        var input = root.querySelector(".final-team-name-input");
        var fallback = root.getAttribute("data-team-name") || "";
        var imageTitle = input
          ? (input.value || "").trim()
          : fallback;
        if (!imageTitle) imageTitle = fallback || "Team";

        var theme = (root.getAttribute("data-theme") || "multi").toLowerCase();
        var bg = THEME_BG[theme] || THEME_BG.multi;

        var scrollY = window.scrollY || 0;
        window.scrollTo(0, 0);

        btn.disabled = true;
        html2canvas(root, {
          scale: 2,
          useCORS: true,
          allowTaint: false,
          backgroundColor: bg,
          logging: false,
          scrollX: 0,
          scrollY: 0,
          onclone: function (doc) {
            replaceTeamTitleInClone(doc, root);
          },
        })
          .then(function (canvas) {
            downloadDataUrl(
              sanitizeFilename(imageTitle) + "-squad.png",
              canvas.toDataURL("image/png")
            );
          })
          .catch(function () {
            window.alert("Could not capture this team. Check images load correctly.");
          })
          .finally(function () {
            window.scrollTo(0, scrollY);
            btn.disabled = false;
          });
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindSave);
  } else {
    bindSave();
  }
})();
