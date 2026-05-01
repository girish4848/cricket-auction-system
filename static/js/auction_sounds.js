/**
 * Auction sounds — CDN clips (Mixkit). Replace URLs or host under /static/sounds/.
 */
(function () {
  var SOUNDS = {
    lot_start:
      "https://assets.mixkit.co/active_storage/sfx/212/212-preview.mp3",
    sold:
      "https://assets.mixkit.co/active_storage/sfx/2019/2019-preview.mp3",
    unsold:
      "https://assets.mixkit.co/active_storage/sfx/2955/2955-preview.mp3",
    bid:
      "https://assets.mixkit.co/active_storage/sfx/2568/2568-preview.mp3",
    heartbeat:
      "https://assets.mixkit.co/active_storage/sfx/270/270-preview.mp3",
    auction_complete:
      "https://assets.mixkit.co/active_storage/sfx/1435/1435-preview.mp3",
  };

  var enabled = true;
  var pool = {};
  var lastHeartbeatTimer = null;

  function unlock() {
    try {
      Object.keys(SOUNDS).forEach(function (key) {
        var a = pool[key];
        if (!a) return;
        a.volume = 0;
        var p = a.play();
        if (p && p.then) {
          p.then(function () {
            a.pause();
            a.currentTime = 0;
            a.volume = volFor(key);
          }).catch(function () {});
        }
      });
    } catch (e) {}
    document.removeEventListener("touchstart", unlock);
    document.removeEventListener("click", unlock);
  }
  document.addEventListener("touchstart", unlock, { once: true, passive: true });
  document.addEventListener("click", unlock, { once: true });

  function volFor(key) {
    if (key === "bid") return 0.3;
    if (key === "heartbeat") return 0.28;
    if (key === "auction_complete") return 0.88;
    return 0.85;
  }

  function getAudio(key) {
    if (!pool[key]) {
      pool[key] = new Audio(SOUNDS[key]);
      pool[key].preload = "auto";
      pool[key].volume = volFor(key);
    }
    return pool[key];
  }

  function play(key) {
    if (!enabled || !SOUNDS[key]) return;
    try {
      var a = getAudio(key);
      a.volume = volFor(key);
      a.currentTime = 0;
      var p = a.play();
      if (p && p.catch) p.catch(function () {});
    } catch (e) {}
  }

  function maybeHeartbeat(data) {
    if (!enabled || !data) return;
    var lim =
      typeof data.heartbeat_last_seconds === "number"
        ? data.heartbeat_last_seconds
        : 10;
    if (
      data.status === "running" &&
      typeof data.timer === "number" &&
      data.timer <= lim &&
      data.timer >= 1
    ) {
      if (data.timer !== lastHeartbeatTimer) {
        lastHeartbeatTimer = data.timer;
        play("heartbeat");
      }
    } else {
      lastHeartbeatTimer = null;
    }
  }

  window.AuctionAudio = {
    play: play,
    setEnabled: function (v) {
      enabled = !!v;
    },
    bindSocket: function (socket) {
      if (!socket || !socket.on) return;

      socket.on("auction_sound", function (payload) {
        if (payload && payload.kind) play(payload.kind);
      });

      socket.on("player_sold", function () {
        play("sold");
      });

      socket.on("player_unsold", function () {
        play("unsold");
      });

      socket.on("auction_state_update", function (data) {
        maybeHeartbeat(data);
      });
    },
  };

  document.addEventListener("DOMContentLoaded", function () {
    if (localStorage.getItem("auction_sound_on") === "0") {
      window.AuctionAudio.setEnabled(false);
    }
    document.querySelectorAll("[data-sound-toggle]").forEach(function (btn) {
      function sync() {
        var on = localStorage.getItem("auction_sound_on") !== "0";
        btn.textContent = on ? "Sound on" : "Muted";
        btn.setAttribute("aria-pressed", on ? "true" : "false");
        window.AuctionAudio.setEnabled(on);
      }
      sync();
      btn.addEventListener("click", function () {
        var on = localStorage.getItem("auction_sound_on") !== "0";
        localStorage.setItem("auction_sound_on", on ? "0" : "1");
        sync();
      });
    });
  });
})();
