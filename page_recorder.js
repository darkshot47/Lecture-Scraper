/* ============================================================================
 * StudyRatna lecture — in-page recorder (aapke apne device ke liye)
 * ============================================================================
 *
 * Kab use karein: jab aap app me lecture **dekh pa rahe hain** (play ho raha hai).
 * GitHub runner US datacenter se 403 khata hai, aapka phone/laptop nahi - isliye
 * recording aapke device par hi karni chahiye.
 *
 * Kaise:
 *   1. Lecture Chrome me kholen (Telegram ke andar se "Open in Chrome" /
 *      "Open in Browser" - 3-dot menu me hota hai; webview me download block
 *      ho sakta hai).
 *   2. Video play karein (zaroori: player ko segments load karne do).
 *   3. Address bar me  chrome://inspect  ... nahi - simple tarika:
 *      laptop par:  F12 -> Console  ->  neeche wala poora code paste karein.
 *      Android par: Chrome me address bar me  javascript:  se shuru karke paste
 *      karna block ho sakta hai, isliye Android par "Eruda" ya laptop + USB
 *      debugging (chrome://inspect) use karein. Sabse aasan: laptop par kholen.
 *   4. Paste karte hi neeche-right ek panel dikhega (progress + Save buttons).
 *   5. Lecture chalti rahe; "Save MP4/WebM" dabayein ya end tak rukne par
 *      auto-save ho jaayega.
 *
 * Kya karta hai (do raste, dono saath chalte hain):
 *   A) MSE hook  - hls.js jo decrypted media `appendBuffer()` me deta hai, wahi
 *      bytes capture karta hai (lossless, compressed). fMP4 mile to .mp4,
 *      TS mile to .ts banta hai.
 *   B) MediaRecorder - video element ke `captureStream()` se WebM banata hai
 *      (MP4 supported ho to .mp4 bhi). Ye wahi cheez hai jo site ka
 *      "Download Video (.webm)" button deta hai - isliye gallery me nahi dikhta:
 *      Android gallery sirf mp4/mkv index karti hai, webm nahi.
 *
 * Ye script kuch decrypt/forge nahi karti: jo player aapke browser me already
 * play kar raha hai, bas usi ko save karti hai.
 * ==========================================================================*/
(function () {
  "use strict";

  if (window.__srRecorder) {
    window.__srRecorder.panel.style.display = "block";
    return "recorder already active";
  }

  const S = {
    mse: [],          /* Uint8Array chunks (decrypted media) */
    mseBytes: 0,
    mseMime: "",
    mseKind: "unknown",   /* mp4 | ts | unknown */
    rec: null,
    recChunks: [],
    recBytes: 0,
    recMime: "",
    startedAt: Date.now(),
    saved: false,
  };

  /* ---------------- A) MSE hook (prototype level: hls.js baad me bhi chale) --- */

  const SB = window.SourceBuffer && window.SourceBuffer.prototype;
  if (SB && SB.appendBuffer && !SB.__srPatched) {
    const orig = SB.appendBuffer;
    SB.appendBuffer = function (buf) {
      try {
        const src = buf.buffer ? buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) : buf;
        const u8 = new Uint8Array(src);
        S.mse.push(u8);
        S.mseBytes += u8.length;
        if (S.mseKind === "unknown" && u8.length > 8) {
          /* fMP4: 'styp'/'ftyp' box; TS: 0x47 sync byte har 188 bytes par */
          const tag = String.fromCharCode(u8[4], u8[5], u8[6], u8[7]);
          if (tag === "styp" || tag === "ftyp" || tag === "moof" || tag === "sidx") S.mseKind = "mp4";
          else if (u8[0] === 0x47) S.mseKind = "ts";
        }
        paint();
      } catch (e) {}
      return orig.call(this, buf);
    };
    SB.__srPatched = true;
  }

  const MS = window.MediaSource && window.MediaSource.prototype;
  if (MS && MS.addSourceBuffer && !MS.__srPatched) {
    const origAdd = MS.addSourceBuffer;
    MS.addSourceBuffer = function (m) {
      if (!S.mseMime) S.mseMime = m;
      return origAdd.call(this, m);
    };
    MS.__srPatched = true;
  }

  /* ---------------- B) MediaRecorder (video element se) ---------------------- */

  function pickRecMime() {
    const list = [
      'video/mp4;codecs="avc1.42E01E,mp4a.40.2"',
      'video/mp4;codecs="avc1.64001E,mp4a.40.2"',
      "video/mp4",
      'video/webm;codecs="vp9,opus"',
      'video/webm;codecs="vp8,opus"',
      "video/webm",
    ];
    for (const m of list) {
      try { if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m; } catch (e) {}
    }
    return "";
  }

  function attachRecorder() {
    if (S.rec) return true;
    const v = document.querySelector("video");
    if (!v || !v.captureStream || !window.MediaRecorder) return false;
    const mime = pickRecMime();
    if (!mime) return false;
    try {
      const stream = v.captureStream();
      const rec = new MediaRecorder(stream, { mimeType: mime, videoBitsPerSecond: 3000000 });
      rec.ondataavailable = (e) => {
        if (e.data && e.data.size) {
          S.recChunks.push(e.data);
          S.recBytes += e.data.size;
          paint();
        }
      };
      rec.start(2000);
      S.rec = rec;
      S.recMime = mime;
      return true;
    } catch (e) {
      return false;
    }
  }
  const attachTimer = setInterval(() => {
    if (attachRecorder()) clearInterval(attachTimer);
  }, 1500);
  setTimeout(() => clearInterval(attachTimer), 60000);

  /* ---------------- UI panel ------------------------------------------------- */

  const panel = document.createElement("div");
  panel.style.cssText = [
    "position:fixed", "right:12px", "bottom:12px", "z-index:2147483647",
    "background:#0b1220", "color:#e5e7eb", "font:12px/1.45 system-ui,sans-serif",
    "border:1px solid #334155", "border-radius:12px", "padding:10px 12px",
    "box-shadow:0 8px 30px rgba(0,0,0,.45)", "min-width:250px", "max-width:320px",
  ].join(";");
  panel.innerHTML = `
    <div style="font-weight:700;margin-bottom:6px">Lecture recorder</div>
    <div id="srLine1">MSE: 0.0 MB</div>
    <div id="srLine2">Recorder: 0.0 MB</div>
    <div id="srLine3" style="opacity:.7">player ka intezaar...</div>
    <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
      <button id="srSaveMse"  style="flex:1;padding:6px 8px;border-radius:8px;border:0;background:#6366f1;color:#fff;font-weight:700;cursor:pointer">Save MSE</button>
      <button id="srSaveRec"  style="flex:1;padding:6px 8px;border-radius:8px;border:0;background:#0ea5e9;color:#fff;font-weight:700;cursor:pointer">Save Rec</button>
    </div>
    <div id="srMsg" style="margin-top:6px;color:#93c5fd"></div>
    <div style="margin-top:6px;opacity:.55;font-size:11px">chalti hui lecture par hi data aata hai</div>`;
  document.body.appendChild(panel);

  const $ = (id) => panel.querySelector("#" + id);
  const mb = (n) => (n / (1024 * 1024)).toFixed(1) + " MB";

  function paint() {
    const v = document.querySelector("video");
    let t = 0, d = 0;
    if (v) { t = v.currentTime || 0; d = isFinite(v.duration) ? v.duration : 0; }
    $("srLine1").textContent = `MSE: ${mb(S.mseBytes)} (${S.mseKind})`;
    $("srLine2").textContent = `Recorder: ${mb(S.recBytes)} ${S.recMime ? "(" + S.recMime.split(";")[0].replace("video/", "") + ")" : ""}`;
    $("srLine3").textContent = v ? `t=${t.toFixed(1)}s${d ? " / " + d.toFixed(0) + "s" : ""}  ${S.rec ? "rec:" + S.rec.state : "rec: off"}` : "video element nahi mila";
    $("srSaveMse").disabled = S.mseBytes === 0;
    $("srSaveRec").disabled = S.recBytes === 0;
  }
  const timer = setInterval(paint, 1000);
  paint();

  /* ---------------- Save ----------------------------------------------------- */

  function download(blob, ext) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `lecture-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.${ext}`;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 60000);
    $("srMsg").textContent = `saved .${ext} (Downloads me dekhein)`;
  }

  $("srSaveMse").onclick = () => {
    if (!S.mseBytes) return;
    const ext = S.mseKind === "mp4" ? "mp4" : "ts";
    const type = S.mseKind === "mp4" ? "video/mp4" : "video/mp2t";
    download(new Blob(S.mse, { type }), ext);
  };

  $("srSaveRec").onclick = () => {
    const isMp4 = /mp4/i.test(S.recMime);
    const finish = () => {
      const blob = new Blob(S.recChunks, { type: isMp4 ? "video/mp4" : "video/webm" });
      download(blob, isMp4 ? "mp4" : "webm");
    };
    if (S.rec && S.rec.state !== "inactive") {
      S.rec.onstop = finish;
      try { S.rec.stop(); } catch (e) { finish(); }
    } else finish();
  };

  // Lecture end par khud save kar do (agar kuch capture hua ho)
  const endWatch = setInterval(() => {
    const v = document.querySelector("video");
    if (!v || S.saved) return;
    if (v.ended && (S.mseBytes > 1e6 || S.recBytes > 1e6)) {
      S.saved = true;
      if (S.recBytes > S.mseBytes) $("srSaveRec").click();
      else $("srSaveMse").click();
    }
  }, 3000);
  setTimeout(() => clearInterval(endWatch), 4 * 60 * 60 * 1000);
  setTimeout(() => clearInterval(timer), 4 * 60 * 60 * 1000);

  window.__srRecorder = { panel, state: S };
  return "recorder ready: play the lecture, then Save";
})();
