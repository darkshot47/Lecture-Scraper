/* ============================================================================
 * Lecture Saver — RECORDER (page ke andar chalta hai)
 * ============================================================================
 * Soch: agar page ke andar video chal rahi hai, to file banane ka sabse
 * bharosa-mand raasta hai USI video ko capture karna. Playlist/key/CORS sab
 * chhod do — jo frames player me aa rahe hain, wahi file me jayenge.
 *
 * Design (zero-dependency):
 *   1. <video> me `captureStream()` daalo (Chrome 111+ me video-only bhi chalta hai)
 *      -> MediaRecorder (MP4 agar supported, warna WebM)
 *   2. Recorder poori length cover kare iske liye video ko **seek seek** ke
 *      chalate hain: currentTime += 6s, thoda ruk ke aage. Playback se ~6-10x tez
 *      aur poora lecture cover.
 *   3. Chunks seedhe AndroidBridge par base64 me jaate hain -> Gallery
 *
 * Fallback: captureStream na ho to MSE appendBuffer chunks (decrypted) copy
 * karte hain, warna sirf real-time recording.
 * ==========================================================================*/
(function () {
  "use strict";
  if (window.__lsRec2) return;
  window.__lsRec2 = true;

  var CFG = {
    seekStep: 6.0,        // har seek me kitne second aage
    settleMs: 420,        // seek ke baad frames aane ka intezaar
    chunkMs: 2000,        // MediaRecorder timeslice
    bitsPerSec: 5000000,  // 5 Mbps — screen record jaisa quality
    maxMs: 45 * 60 * 1000 // safety: 45 min tak hi
  };

  var S = {
    rec: null,
    mime: "",
    kind: "unknown",
    bytes: 0,
    chunks: 0,
    running: false,
    finished: false,
    err: "",
    duration: 0,
    pos: 0,
    mode: "none",
    mse: [],          // fallback: decrypted fMP4/TS
    mseBytes: 0
  };

  function send(msg) {
    try {
      if (window.AndroidBridge && window.AndroidBridge.postMessage) {
        window.AndroidBridge.postMessage(JSON.stringify(msg));
      }
    } catch (e) {}
  }

  function log(m) {
    send({ type: "log", msg: "REC " + m });
  }

  /* ---------------------------------------------------------- B) MSE backup */
  var SB = window.SourceBuffer && window.SourceBuffer.prototype;
  if (SB && SB.appendBuffer && !SB.__ls2) {
    var origAppend = SB.appendBuffer;
    SB.appendBuffer = function (buf) {
      try {
        var src = buf.buffer ? buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) : buf;
        var u8 = new Uint8Array(src);
        S.mse.push(u8);
        S.mseBytes += u8.length;
        // decoded bytes disk par (key/capture ke bina bhi file ban jaye)
        try {
          var CH = 32000;
          for (var o = 0; o < u8.length; o += CH) {
            var sl = u8.subarray(o, Math.min(o + CH, u8.length));
            var bin = "";
            for (var j = 0; j < sl.length; j++) bin += String.fromCharCode(sl[j]);
            send({ type: "mse-chunk", b64: btoa(bin), last: (o + CH >= u8.length) });
          }
        } catch (e) {}
        if (S.kind === "unknown" && u8.length > 8) {
          var tag = String.fromCharCode(u8[4], u8[5], u8[6], u8[7]);
          if (tag === "styp" || tag === "moof" || tag === "ftyp" || tag === "sidx") S.kind = "mp4";
          else if (u8[0] === 0x47) S.kind = "ts";
        }
      } catch (e) {}
      return origAppend.call(this, buf);
    };
    SB.__ls2 = true;
  }

  function pickMime() {
    var list = [
      'video/mp4;codecs="avc1.42E01E"',
      'video/mp4;codecs="avc1.64001E"',
      "video/mp4",
      'video/webm;codecs="vp9"',
      'video/webm;codecs="vp8"',
      "video/webm"
    ];
    for (var i = 0; i < list.length; i++) {
      try {
        if (window.MediaRecorder && MediaRecorder.isTypeSupported(list[i])) return list[i];
      } catch (e) {}
    }
    return "";
  }

  function pickVideo() {
    var vs = Array.prototype.slice.call(document.querySelectorAll("video"));
    var best = null;
    for (var i = 0; i < vs.length; i++) {
      var v = vs[i];
      var area = (v.videoWidth || 0) * (v.videoHeight || 0);
      if (!best || area > (best.videoWidth || 0) * (best.videoHeight || 0)) best = v;
    }
    return best;
  }

  /* ---------------------------------------------------------- C) recording --- */

  function toB64(u8) {
    var bin = "", CH = 8192;
    for (var p = 0; p < u8.length; p += CH) {
      bin += String.fromCharCode.apply(null, u8.subarray(p, p + CH));
    }
    return btoa(bin);
  }

  /**
   * AUDIO fallback: kuch WebView me video frames black aate hain par audio
   * track zinda rehta hai. Tab bhi lecture kaam ka hai — audio save karo.
   */
  function startAudioOnly() {
    var v = pickVideo();
    if (!v || !v.captureStream) return false;
    try {
      var st = v.captureStream(1);
      var at = st.getAudioTracks();
      if (!at.length) { log("audio track nahi"); return false; }
      var mime = "audio/webm";
      try { if (MediaRecorder.isTypeSupported("audio/mp4")) mime = "audio/mp4"; } catch (e) {}
      var rec = new MediaRecorder(new MediaStream(at), { mimeType: mime, audioBitsPerSecond: 128000 });
      rec.ondataavailable = function (e) {
        if (!e.data || !e.data.size) return;
        S.chunks++;
        var fr = new FileReader();
        fr.onloadend = function () {
          var b64 = String(fr.result).split(",")[1] || "";
          if (!b64) return;
          S.bytes += e.data.size;
          send({ type: "rec", b64: b64, mime: mime, bytes: S.bytes, done: rec.state === "inactive" });
        };
        fr.readAsDataURL(e.data);
      };
      rec.onstop = function () {
        S.running = false;
        if (!S.finished) {
          S.finished = true;
          send({ type: "rec-done", ok: S.bytes > 50000, bytes: S.bytes, mime: mime });
        }
      };
      rec.start(CFG.chunkMs);
      S.rec = rec;
      S.mime = mime;
      S.kind = "audio";
      S.running = true;
      S.mode = "audio";
      S.duration = isFinite(v.duration) ? v.duration : 0;
      log("AUDIO mode (" + mime + ")");
      return true;
    } catch (e) {
      log("audio fail: " + e);
      return false;
    }
  }

  /** video ki asli state — black screen ka pata lagane ke liye */
  window.__lsProbe = function () {
    var v = pickVideo();
    if (!v) return JSON.stringify({ video: false });
    var cs = false, at = 0, vt = 0;
    try {
      if (v.captureStream) {
        var st = v.captureStream(1);
        cs = true;
        at = st.getAudioTracks().length;
        vt = st.getVideoTracks().length;
      }
    } catch (e) {}
    return JSON.stringify({
      video: true,
      w: v.videoWidth || 0,
      h: v.videoHeight || 0,
      ready: v.readyState,
      paused: v.paused,
      t: v.currentTime || 0,
      dur: isFinite(v.duration) ? v.duration : 0,
      hasCaptureStream: !!v.captureStream,
      audioTracks: at,
      videoTracks: vt,
      mse: !!window.MediaSource,
      canHls: !!v.canPlayType && !!v.canPlayType("application/vnd.apple.mpegurl"),
      src: (v.currentSrc || v.src || "").slice(0, 90)
    });
  };

  function startRecorder() {
    if (S.rec) return true;
    var v = pickVideo();
    if (!v) { log("video element nahi mila"); return false; }
    if (!v.captureStream) { log("captureStream support nahi"); return false; }
    var mime = pickMime();
    if (!mime) { log("MediaRecorder support nahi"); return false; }

    var stream;
    try {
      stream = v.captureStream(30);
    } catch (e) {
      log("captureStream fail: " + e);
      return false;
    }
    if (!stream || !stream.getVideoTracks().length) { log("stream me video track nahi"); return false; }

    try {
      var rec = new MediaRecorder(stream, { mimeType: mime, videoBitsPerSecond: CFG.bitsPerSec });
      rec.ondataavailable = function (e) {
        if (!e.data || !e.data.size) return;
        S.chunks++;
        var fr = new FileReader();
        fr.onloadend = function () {
          var b64 = String(fr.result).split(",")[1] || "";
          if (!b64) return;
          S.bytes += e.data.size;
          send({ type: "rec", b64: b64, mime: mime, bytes: S.bytes, done: rec.state === "inactive" });
        };
        fr.readAsDataURL(e.data);
      };
      rec.onstop = function () {
        S.running = false;
        if (!S.finished) {
          S.finished = true;
          send({ type: "rec-done", ok: S.bytes > 200000, bytes: S.bytes, mime: mime });
        }
      };
      rec.start(CFG.chunkMs);
      S.rec = rec;
      S.mime = mime;
      S.kind = mime.indexOf("mp4") >= 0 ? "mp4" : "webm";
      S.running = true;
      S.mode = "captureStream";
      S.duration = isFinite(v.duration) ? v.duration : 0;
      log("chalu (" + mime + ", duration " + Math.round(S.duration) + "s)");
      return true;
    } catch (e) {
      log("MediaRecorder start fail: " + e);
      return false;
    }
  }

  /**
   * Poora lecture cover karne ke liye seek-seek ke aage badho.
   * Har step par ~0.42s ruk kar frames ko aane dete hain, phir agla seek.
   */
  function drive() {
    var v = pickVideo();
    if (!v) return;
    if (S.finished) return;
    var dur = isFinite(v.duration) && v.duration > 0 ? v.duration : S.duration;
    if (dur > 0) S.duration = dur;
    if (S.duration > 0 && v.currentTime >= S.duration - 0.5) {
      finish("end");
      return;
    }
    var next = Math.min(v.currentTime + CFG.seekStep, S.duration > 0 ? S.duration - 0.2 : v.currentTime + CFG.seekStep);
    if (next <= v.currentTime + 0.05) {
      // aage nahi badh raha (live/buffering) — thoda wait
      setTimeout(drive, 1200);
      return;
    }
    try {
      v.currentTime = next;
      S.pos = next;
    } catch (e) {}
    setTimeout(drive, CFG.settleMs);
  }

  function finish(why) {
    if (S.finished) return;
    S.finished = true;
    log("finish: " + why + " · " + (S.bytes / 1048576).toFixed(1) + " MB");
    try {
      if (S.rec && S.rec.state !== "inactive") S.rec.stop();
    } catch (e) {}
  }

  /* --------------------------------------------------------------- D) API --- */

  window.__lsRecStart = function (opts) {
    if (opts) {
      if (opts.seekStep) CFG.seekStep = opts.seekStep;
      if (opts.bitsPerSec) CFG.bitsPerSec = opts.bitsPerSec;
    }
    if (!startRecorder()) {
      if (startAudioOnly()) {
        var vv = pickVideo();
        try { if (vv) { vv.muted = true; var pp = vv.play(); if (pp && pp.catch) pp.catch(function () {}); } } catch (e) {}
        setTimeout(drive, 900);
        return 2;
      }
      // captureStream nahi -> MSE copy par fallback (sirf wahi jo ab tak chal chuka)
      S.mode = "mse";
      S.duration = 0;
      S.finished = false;
      log("recorder nahi bana — MSE copy mode");
      return 0;
    }
    var v = pickVideo();
    try {
      if (v) {
        v.muted = true;
        var p = v.play();
        if (p && p.catch) p.catch(function () {});
      }
    } catch (e) {}
    setTimeout(drive, 900);
    return 1;
  };

  window.__lsRecStop = function () {
    finish("manual");
    return S.bytes;
  };

  window.__lsRecStatus = function () {
    var v = pickVideo();
    return JSON.stringify({
      mode: S.mode,
      running: S.running,
      bytes: S.bytes,
      chunks: S.chunks,
      mime: S.mime,
      kind: S.kind,
      pos: v ? v.currentTime || 0 : 0,
      duration: S.duration || (v && isFinite(v.duration) ? v.duration : 0),
      mseBytes: S.mseBytes,
      finished: S.finished,
      err: S.err
    });
  };

  /** MSE fallback dump (chunked) */
  window.__lsStartDump = function () {
    if (!S.mseBytes) { send({ type: "mse-done", ok: false }); return 0; }
    var starts = [], acc = 0, i;
    for (i = 0; i < S.mse.length; i++) { starts.push(acc); acc += S.mse[i].length; }
    var total = acc, SLICE = 2 * 1024 * 1024, off = 0;

    function readRange(from, to) {
      var out = new Uint8Array(to - from), wrote = 0;
      for (var k = 0; k < S.mse.length; k++) {
        var cStart = starts[k], cLen = S.mse[k].length, cEnd = cStart + cLen;
        if (cEnd <= from) continue;
        if (cStart >= to) break;
        var s0 = Math.max(from, cStart) - cStart;
        var s1 = Math.min(to, cEnd) - cStart;
        var part = S.mse[k].subarray(s0, s1);
        out.set(part, wrote);
        wrote += part.length;
      }
      return out;
    }

    function step() {
      var end = Math.min(off + SLICE, total);
      send({ type: "mse", b64: toB64(readRange(off, end)), last: end >= total, kind: S.kind });
      off = end;
      if (off < total) setTimeout(step, 0);
      else send({ type: "mse-done", ok: true });
    }
    step();
    return total;
  };

  window.__lsPoke = function () {
    try {
      var v = pickVideo();
      if (v) { v.muted = true; var p = v.play(); if (p && p.catch) p.catch(function () {}); }
    } catch (e) {}
    return 1;
  };

  send({ type: "ready2" });
})();
