/* page_recorder.js ka off-line test (jsdom). Chalane ke liye:
 *   npm install jsdom && node tools/test_page_recorder.js
 * Fake MSE + MediaRecorder + video element banata hai, recorder inject karta hai,
 * chunks capture karke Save dabata hai, aur blob ka type/size verify karta hai. */
const fs = require('fs');
const { JSDOM } = require('jsdom');
const code = fs.readFileSync(require('path').join(__dirname, '..', 'console_paste.txt'), 'utf8');

const dom = new JSDOM(`<!doctype html><html><body><video id="v"></video></body></html>`,
  { runScripts: 'outside-only', pretendToBeVisual: true, url: 'https://app.studyratna.org/?playerToken=x' });
const { window } = dom;

// --- fake media stack, mirroring Chrome + hls.js ---------------------------
function SourceBuffer() {}
SourceBuffer.prototype.appendBuffer = function (buf) { window.__orig = (window.__orig || 0) + buf.byteLength; };
window.SourceBuffer = SourceBuffer;

function MediaSource() {}
MediaSource.isTypeSupported = () => true;
MediaSource.prototype.addSourceBuffer = function (m) {
  window.__mime = m; const sb = new SourceBuffer(); this.__sb = sb; return sb;
};
window.MediaSource = MediaSource;

function MediaRecorder(stream, opts) { this.state='recording'; this.mimeType=opts.mimeType; window.__recMime=opts.mimeType; }
MediaRecorder.prototype.start = function () {};
MediaRecorder.prototype.stop = function () { this.state='inactive'; if (this.onstop) this.onstop(); };
MediaRecorder.isTypeSupported = (t) => /mp4/.test(t) || /webm/.test(t);
window.MediaRecorder = MediaRecorder;

const video = window.document.getElementById('v');
video.captureStream = () => ({ fake: true });
Object.defineProperty(video, 'currentTime', { get: () => 12.5 });
Object.defineProperty(video, 'duration', { get: () => 1001 });

let saved = null;
window.URL.createObjectURL = (blob) => { saved = blob; return 'blob:fake'; };
window.URL.revokeObjectURL = () => {};

console.log('eval:', window.eval(code));
console.log('panel:', !!window.document.querySelector('#srLine1'));

// hls.js jaisa behaviour: addSourceBuffer, phir decrypted fMP4 append
const sb = new window.MediaSource().addSourceBuffer('video/mp4; codecs="avc1.42c01e"');
const fmp4 = new Uint8Array(4096); fmp4[4]=0x73; fmp4[5]=0x74; fmp4[6]=0x79; fmp4[7]=0x70;
sb.appendBuffer(fmp4); sb.appendBuffer(fmp4); sb.appendBuffer(fmp4);

setTimeout(() => {
  const st = window.__srRecorder.state;
  console.log('captured bytes :', st.mseBytes, '| kind:', st.mseKind, '| orig passthrough:', window.__orig);
  console.log('recorder attached:', !!st.rec, st.recMime || '(none)', '| state:', st.rec && st.rec.state);
  window.document.querySelector('#srSaveMse').click();
  console.log('Save MSE -> blob:', saved && saved.type, saved && saved.size, 'bytes');
  console.log('line1:', window.document.querySelector('#srLine1').textContent);
  console.log('msg  :', window.document.querySelector('#srMsg').textContent);
  dom.window.close();
  process.exit(0);
}, 2200);
