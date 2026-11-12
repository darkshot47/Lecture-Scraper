/* ============================================================================
 * Lecture Saver - WebView ke andar chalne wala recorder
 * ============================================================================
 * Ye file Android app ke assets me jaati hai (build_console.py)
 * aur page load hone se pehle inject hoti hai. Ye wahi logic hai jo
 * page_recorder.js me hai, bas:
 *   - parent Android ko message bhejta hai (AndroidBridge.postMessage)
 *   - save panel nahi banata (app ka apna UI hai)
 *   - chunks base64 me chunks-dump ke through app ko deta hai
 * ==========================================================================*/
(function () {
  "use strict";
  if (window.__lectureSaver) return;

  var S = {
    mse: [],
    mseBytes: 0,
    mseMime: "",
    mseKind: "unknown",
    rec: null,
    recChunks: [],
    recBytes: 0,
    recMime: "",
    pending: "",      // MediaRecorder ka binary, base64 me jaata hai
    dumping: false,
  };

  function send(msg) {
    try {
      if (window.AndroidBridge && window.AndroidBridge.postMessage) {
        window.AndroidBridge.postMessage(JSON.stringify(msg));
      }
    } catch (e) {}
  }

  /* ---------------- A) MSE hook (decrypted hls.js media) ------------------- */
  var SB = window.SourceBuffer && window.SourceBuffer.prototype;
  if (SB && SB.appendBuffer && !SB.__lsPatched) {
    var origAppend = SB.appendBuffer;
    SB.appendBuffer = function (buf) {
      try {
        var src = buf.buffer
          ? buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength)
          : buf;
        var u8 = new Uint8Array(src);
        S.mse.push(u8);
        S.mseBytes += u8.length;
        if (S.mseKind === "unknown" && u8.length > 8) {
          var tag = String.fromCharCode(u8[4], u8[5], u8[6], u8[7]);
          if (tag === "styp" || tag === "ftyp" || tag === "moof" || tag === "sidx") {
            S.mseKind = "mp4";
          } else if (u8[0] === 0x47) {
            S.mseKind = "ts";
          }
        }
      } catch (e) {}
      return origAppend.call(this, buf);
    };
    SB.__lsPatched = true;
  }

  var MS = window.MediaSource && window.MediaSource.prototype;
  if (MS && MS.addSourceBuffer && !MS.__lsPatched) {
    var origAdd = MS.addSourceBuffer;
    MS.addSourceBuffer = function (m) {
      if (!S.mseMime) S.mseMime = m;
      return origAdd.call(this, m);
    };
    MS.__lsPatched = true;
  }

  /* ---------------- B) MediaRecorder fallback ------------------------------ */
  function pickMime() {
    var list = [
      'video/mp4;codecs="avc1.42E01E,mp4a.40.2"',
      'video/mp4;codecs="avc1.64001E,mp4a.40.2"',
      "video/mp4",
      'video/webm;codecs="vp9,opus"',
      'video/webm;codecs="vp8,opus"',
      "video/webm",
    ];
    for (var i = 0; i < list.length; i++) {
      try {
        if (window.MediaRecorder && MediaRecorder.isTypeSupported(list[i])) return list[i];
      } catch (e) {}
    }
    return "";
  }

  function attach() {
    if (S.rec) return true;
    var v = document.querySelector("video");
    if (!v || !v.captureStream || !window.MediaRecorder) return false;
    var mime = pickMime();
    if (!mime) return false;
    try {
      var rec = new MediaRecorder(v.captureStream(), {
        mimeType: mime,
        videoBitsPerSecond: 3000000,
      });
      rec.ondataavailable = function (e) {
        if (!e.data || !e.data.size) return;
        S.recChunks.push(e.data);
        S.recBytes += e.data.size;
        var rd = new FileReader();
        rd.onloadend = function () {
          var b64 = String(rd.result).split(",")[1] || "";
          send({ type: "rec", b64: b64, done: rec.state === "inactive" });
        };
        rd.readAsDataURL(e.data);
      };
      rec.start(2000);
      S.rec = rec;
      S.recMime = mime;
      send({ type: "log", msg: "recorder attached: " + mime });
      return true;
    } catch (e) {
      send({ type: "log", msg: "recorder attach fail: " + e });
      return false;
    }
  }
  var attachTimer = setInterval(function () {
    if (attach()) clearInterval(attachTimer);
  }, 1500);

  /* ---------------- Player ko play karne ki koshish ------------------------ */
  function poke() {
    try {
      document.querySelectorAll("video").forEach(function (v) {
        v.muted = true;
        var p = v.play();
        if (p && p.catch) p.catch(function () {});
      });
    } catch (e) {}
    var sels = ["button[aria-label*='play' i]", ".vjs-big-play-button",
                "[class*='play-button' i]", "[class*='playButton' i]", "video"];
    for (var i = 0; i < sels.length; i++) {
      try {
        var el = document.querySelector(sels[i]);
        if (el) { el.click(); break; }
      } catch (e) {}
    }
  }
  setInterval(poke, 4000);

  /* ---------------- Status + dump (app inhe call karta hai) ---------------- */
  window.__lsStatus = function () {
    var v = document.querySelector("video");
    var st = {
      mseBytes: S.mseBytes,
      mseKind: S.mseKind,
      mseMime: S.mseMime,
      recBytes: S.recBytes,
      recMime: S.recMime,
      recState: S.rec ? S.rec.state : null,
      current: v ? v.currentTime || 0 : 0,
      duration: v && isFinite(v.duration) ? v.duration : 0,
      ended: v ? !!v.ended : false,
    };
    return JSON.stringify(st);
  };

  /* MSE chunks base64 me nikaalo (bade buffer ke liye chunked). */
  window.__lsDumpMse = function () {
    if (!S.mseBytes || S.dumping) return "";
    S.dumping = true;
    var total = 0, i;
    for (i = 0; i < S.mse.length; i++) total += S.mse[i].length;
    var merged = new Uint8Array(total), off = 0;
    for (i = 0; i < S.mse.length; i++) { merged.set(S.mse[i], off); off += S.mse[i].length; }
    var bin = "", CH = 8192;
    for (var p = 0; p < merged.length; p += CH) {
      bin += String.fromCharCode.apply(null, merged.subarray(p, p + CH));
    }
    S.dumping = false;
    return btoa(bin);
  };

  window.__lsClearMse = function () { S.mse = []; S.mseBytes = 0; };

  window.__lsRecStop = function () {
    try { if (S.rec && S.rec.state !== "inactive") S.rec.stop(); } catch (e) {}
  };

  window.__lectureSaver = S;
  send({ type: "ready" });
})();
