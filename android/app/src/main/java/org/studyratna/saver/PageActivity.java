package org.studyratna.saver;

import android.app.Activity;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.io.File;

/**
 * PageActivity — asli page WebView me, aur teen kaam:
 *
 *  1. PAGE NORMALLY CHALTA HAI — user khud lecture play kar sakta hai (yahi
 *     context hai jahan sab kuch kaam karta hai: cookies, headers, 200s).
 *  2. "Page se download" — Downloader.runViaPage(): playlist/segments/key sab
 *     page ke andar se (same origin) maange jaate hain, isliye Cloudflare 502
 *     aur key 401 dono se bachte hain.
 *  3. SITE KA DOWNLOAD BUTTON — page jab download trigger kare (blob: URL,
 *     data:, ya normal link), hum usse pakad ke Gallery me daal dete hain.
 *     Isse wahi raasta milta hai jo aapke dusre browser me chalta hai.
 */
public class PageActivity extends Activity {

    public static final String EXTRA_URL = "url";

    private WebView web;
    private TextView bar;
    private Button pageDlBtn;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private boolean downloading = false;
    private Button recBtn;
    private boolean recording = false;
    private boolean finishedOnce = false;

    // recorder chunks -> Gallery (stream me, memory me nahi)
    private android.net.Uri recUri;
    private java.io.OutputStream recOut;
    private long recBytes = 0;
    private String recMime = "video/webm";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        String url = getIntent() != null ? getIntent().getStringExtra(EXTRA_URL) : null;
        if (url == null || url.isEmpty()) url = "https://app.studyratna.org/";

        FrameLayout root = new FrameLayout(this);

        web = new WebView(this);
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        /* ---- top bar ---- */
        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.VERTICAL);
        top.setBackgroundColor(0xF2000000);

        LinearLayout row1 = new LinearLayout(this);
        row1.setOrientation(LinearLayout.HORIZONTAL);
        row1.setGravity(Gravity.CENTER_VERTICAL);

        bar = new TextView(this);
        bar.setTextColor(Color.WHITE);
        bar.setTextSize(11f);
        bar.setPadding(14, 8, 8, 8);
        bar.setText("page khul raha hai…");
        row1.addView(bar, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        row1.addView(btn("Band", v -> finish()));
        top.addView(row1);

        LinearLayout row2 = new LinearLayout(this);
        row2.setOrientation(LinearLayout.HORIZONTAL);
        pageDlBtn = btn("⬇ Page se download", v -> pageDownload());
        pageDlBtn.setTextSize(12f);
        pageDlBtn.setLayoutParams(new LinearLayout.LayoutParams(0,
                (int) (42 * getResources().getDisplayMetrics().density), 1f));
        row2.addView(pageDlBtn);
        recBtn = btn("⏺ REC", v -> toggleRecord());
        recBtn.setTextSize(12f);
        row2.addView(recBtn);
        row2.addView(btn("Save", v -> stopRecordAndSave()));
        row2.addView(btn("Copy log", v -> copyLog()));
        top.addView(row2);

        root.addView(top, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.TOP));

        setContentView(root);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setUserAgentString(s.getUserAgentString() + " LectureSaver/2.1");
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        }
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public android.webkit.WebResourceResponse shouldInterceptRequest(
                    WebView view, WebResourceRequest req) {
                NetSniffer.record(view, req);
                return null;   // request ko rokо nahi
            }

            @Override
            public void onPageFinished(WebView view, String u) {
                NetSniffer.absorbCookiesFrom(u);
                // recorder JS (backup capture) + download-catcher
                injectAsset("inject_webview.js");
                injectAsset("recorder.js");
                BrowserNet.attach(web);
                BrowserNet.eval(DL_CATCHER_JS);
                BrowserNet.attach(web);
                // lecture play hone ka intezaar, phir REC khud shuru
                ui.postDelayed(() -> {
                    String raw = getIntent() != null ? getIntent().getStringExtra(EXTRA_URL) : null;
                    if (raw != null && !raw.isEmpty()) web.evaluateJavascript(
                            "(function(){try{window.__lsPoke&&window.__lsPoke();}catch(e){}})()", null);
                    if (!recording && !finishedOnce) toggleRecord();
                }, 9000);
            }
        });

        web.setWebChromeClient(new WebChromeClient());
        web.addJavascriptInterface(new RecBridge(), "AndroidBridge");

        /* ---- site ka download button / blob: ---- */
        web.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String u, String userAgent, String contentDisposition,
                                        String mimeType, long contentLength) {
                handlePageDownload(u, mimeType, contentDisposition);
            }
        });

        BrowserNet.attach(web);
        web.loadUrl(url);
        ui.postDelayed(tickRunnable, 1200);
    }

    /* ------------------------------------------------------- page ka button -- */

    /**
     * Page jab koi download trigger kare: blob:/data: URL ho to JS se bytes
     * uthate hain (WebView khud blob download nahi karta), warna DownloadManager
     * ya browser intent.
     */
    private void handlePageDownload(String u, String mime, String disposition) {
        if (u == null) return;
        if (u.startsWith("blob:")) {
            bar.setText("site ka download pakda (blob) — bytes nikaal rahe hain…");
            jsDownloadBlob(u, mime);
            return;
        }
        if (u.startsWith("data:")) {
            bar.setText("site ka download pakda (data:) — save kar rahe hain…");
            saveBase64FromDataUrl(u);
            return;
        }
        // normal http(s) download — DownloadManager (cookies ke saath)
        try {
            String cookies = CookieManager.getInstance().getCookie(u);
            DownloadManager.Request r = new DownloadManager.Request(Uri.parse(u));
            r.setMimeType(mime);
            r.addRequestHeader("Cookie", cookies);
            r.addRequestHeader("User-Agent", web.getSettings().getUserAgentString());
            String name = android.webkit.URLUtil.guessFileName(u, disposition, mime);
            r.setTitle(name);
            r.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            r.setDestinationInExternalPublicDir(Environment.DIRECTORY_MOVIES + "/LectureSaver", name);
            DownloadManager dm = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
            if (dm != null) dm.enqueue(r);
            bar.setText("download shuru: " + name);
        } catch (Exception e) {
            bar.setText("download manager fail: " + e.getMessage());
            openExternal(u);
        }
    }

    private void openExternal(String u) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(u)));
        } catch (ActivityNotFoundException e) {
            toast("koi app nahi mila is link ke liye");
        }
    }

    /** blob: URL ke bytes JS se nikaal ke base64 me le aao. */
    private void jsDownloadBlob(String blobUrl, String mime) {
        String js = "(function(){"
                + "fetch(" + jsStr(blobUrl) + ").then(function(r){return r.arrayBuffer();})"
                + ".then(function(ab){var u8=new Uint8Array(ab),CH=33000;"
                + "for(var o=0;o<u8.length;o+=CH){"
                + "var s='',p=u8.subarray(o,Math.min(o+CH,u8.length));"
                + "for(var i=0;i<p.length;i++)s+=String.fromCharCode(p[i]);"
                + "try{window.BrowserNetBridge.chunk(200,btoa(s),o+CH>=u8.length,'blob');}catch(e){}}"
                + "if(!u8.length){try{window.BrowserNetBridge.chunk(200,'',true,'blob');}catch(e){}}"
                + "}).catch(function(e){try{window.BrowserNetBridge.chunk(0,'',true,'blob:'+e);}catch(x){}});"
                + "})();";
        final String fMime = mime;
        BrowserNet.BridgeCollect.collect(js, (status, body) -> {
            if (status == 200 && body.length > 1000) {
                String name = "lecture-" + System.currentTimeMillis()
                        + (fMime != null && fMime.contains("webm") ? ".webm"
                        : fMime != null && fMime.contains("mp4") ? ".mp4" : ".ts");
                boolean ok = GalleryWriter.save(this, body, name, fMime);
                bar.setText(ok ? "✓ Gallery me save: " + name : "save fail");
            } else {
                bar.setText("blob nikaalne me fail (HTTP " + status + ")");
            }
        });
    }

    private void saveBase64FromDataUrl(String dataUrl) {
        try {
            int comma = dataUrl.indexOf(',');
            String meta = dataUrl.substring(5, comma);
            String b64 = dataUrl.substring(comma + 1);
            byte[] data = android.util.Base64.decode(b64, android.util.Base64.DEFAULT);
            String mime = meta.split(";")[0];
            String name = "lecture-" + System.currentTimeMillis()
                    + (mime.contains("webm") ? ".webm" : mime.contains("mp4") ? ".mp4" : ".bin");
            boolean ok = GalleryWriter.save(this, data, name, mime);
            bar.setText(ok ? "✓ Gallery me save: " + name : "save fail");
        } catch (Exception e) {
            bar.setText("data: save fail: " + e.getMessage());
        }
    }

    /* --------------------------------------------------------- page-download -- */

    /** Token nikaal ke page ke andar se poora lecture download. */
    private void pageDownload() {
        if (downloading) {
            toast("pehle se chal raha hai");
            return;
        }
        downloading = true;
        pageDlBtn.setEnabled(false);
        bar.setText("token page se nikaal rahe hain…");

        // token: URL me ho sakta hai, ya page ke localStorage/JSON me
        String js = "(function(){"
                + "try{"
                + " var t=new URLSearchParams(location.search).get('playerToken');"
                + " if(t)return 'URL:'+t;"
                + " var ls=[];"
                + " for(var i=0;i<localStorage.length;i++){var k=localStorage.key(i),v=localStorage.getItem(k);"
                + "   if(v&&v.indexOf('playerToken')>=0){var m=v.match(/playerToken=([^&\"'\\\\s]+)/);if(m)return 'LS:'+decodeURIComponent(m[1]);}}"
                + " var h=document.documentElement.innerHTML.match(/playerToken=([^&\"'\\\\s]+)/);"
                + " if(h)return 'HTML:'+decodeURIComponent(h[1]);"
                + " return '';"
                + "}catch(e){return 'ERR:'+e;}})();";
        web.evaluateJavascript(js, value -> {
            String raw = unquote(value);
            String token = raw;
            if (raw.startsWith("URL:") || raw.startsWith("LS:") || raw.startsWith("HTML:")) {
                token = raw.substring(raw.indexOf(':') + 1);
            }
            if (token == null || token.isEmpty() || raw.startsWith("ERR:")) {
                bar.setText("page se token nahi mila — lecture khol ke play karein, phir dabayein");
                downloading = false;
                pageDlBtn.setEnabled(true);
                return;
            }
            runPageDownload(token);
        });
    }

    private void runPageDownload(String tokenRaw) {
        final Token[] holder = new Token[1];
        try {
            holder[0] = Token.parse(tokenRaw);
        } catch (Exception e) {
            bar.setText("token: " + e.getMessage());
            downloading = false;
            pageDlBtn.setEnabled(true);
            return;
        }
        final Token t = holder[0];
        bar.setText("page se download shuru: " + t.label());

        new Thread(() -> {
            try {
                Downloader dl = new Downloader(this, new Downloader.Listener() {
                    @Override
                    public void onStage(String stage) {
                        ui.post(() -> bar.setText(stage));
                    }

                    @Override
                    public void onProgress(int done, int total, long bytes) {
                        ui.post(() -> bar.setText(String.format(java.util.Locale.US,
                                "page download %d/%d · %.1f MB", done, total, bytes / 1048576.0)));
                    }

                    @Override
                    public void onSegmentError(int index, String msg) {
                        ui.post(() -> bar.setText("seg " + index + ": " + msg));
                    }
                });
                dl.extraHeaders.putAll(NetSniffer.sniffedAuthHeaders());
                Downloader.Result r = dl.runViaPage(t);

                if (r.ok()) {
                    String name = "lecture-" + System.currentTimeMillis() + ".ts";
                    byte[] data = Downloader.readAll(r.file);
                    boolean saved = GalleryWriter.save(PageActivity.this, data, name, "video/mp2t");
                    ui.post(() -> bar.setText(saved
                            ? "✓ Gallery me save: Movies/LectureSaver/" + name
                            + " (" + (r.bytes / 1048576.0) + " MB)"
                            : "download hua par Gallery save fail"));
                } else if (r.keyMissing) {
                    ui.post(() -> {
                        StringBuilder sb = new StringBuilder("page se key nahi mili:\n");
                        for (String s : r.keyDiag) sb.append("  · ").append(s).append("\n");
                        sb.append("Abhi lecture play karke 30s rukein, phir dobara dabayein.");
                        bar.setText(sb.toString());
                    });
                } else {
                    ui.post(() -> bar.setText("page download: segments nahi mile (fail=" + r.failed + ")"));
                }
            } catch (Exception e) {
                ui.post(() -> bar.setText("page download fail: " + e.getMessage()));
            } finally {
                downloading = false;
                ui.post(() -> pageDlBtn.setEnabled(true));
            }
        }).start();
    }


    /* ------------------------------------------------------------- RECORDER -- */

    /**
     * REC: page ke andar jo video chal rahi hai use capture karo.
     * Isme playlist/key/CORS ka koi role nahi — jo frames chalti hain wahi file
     * me jaati hain, isliye 502/401 yahan aa hi nahi sakte.
     */
    private void toggleRecord() {
        if (recording) {
            stopRecordAndSave();
            return;
        }
        recBytes = 0;
        recMime = "video/webm";
        web.evaluateJavascript(
                "(function(){try{return window.__lsRecStart?window.__lsRecStart():0;}catch(e){return -1;}})()",
                v -> {
                    String r = v == null ? "" : v.replace("\"", "");
                    if ("1".equals(r)) {
                        recording = true;
                        recBtn.setText("⏹ Stop");
                        bar.setText("REC chalu — lecture ke aage-peeche seek kar raha hoon…");
                        ui.postDelayed(recPoll, 1500);
                    } else {
                        bar.setText("recorder shuru nahi hua (captureStream support nahi?) — "
                                + "lecture play karke 10s baad dobara dabayein");
                    }
                });
    }

    private final Runnable recPoll = new Runnable() {
        @Override
        public void run() {
            if (web == null) return;
            web.evaluateJavascript(
                    "(function(){try{return window.__lsRecStatus?window.__lsRecStatus():null;}catch(e){return null;}})()",
                    PageActivity.this::onRecStatus);
            if (recording) ui.postDelayed(this, 1500);
        }
    };

    private void onRecStatus(String value) {
        String v = unquote(value);
        if (v.isEmpty() || "null".equals(v)) return;
        try {
            org.json.JSONObject o = new org.json.JSONObject(v);
            double pos = o.optDouble("pos", 0);
            double dur = o.optDouble("duration", 0);
            recBytes = o.optLong("bytes", recBytes);
            String mime = o.optString("mime", recMime);
            if (mime != null && !mime.isEmpty()) recMime = mime;
            if (!recording) return;
            bar.setText(String.format(java.util.Locale.US,
                    "REC %.1f MB · %s%.0fs / %.0fs", recBytes / 1048576.0,
                    o.optBoolean("finished") ? "complete " : "", pos, dur));
            if (o.optBoolean("finished")) stopRecordAndSave();
        } catch (Exception ignored) {
        }
    }

    private void stopRecordAndSave() {
        if (!recording) {
            toast("recording chalu nahi");
            return;
        }
        recording = false;
        recBtn.setText("⏺ REC");
        web.evaluateJavascript(
                "(function(){try{window.__lsRecStop&&window.__lsRecStop();}catch(e){}})()", null);
        // JS se aakhri chunks aane ke baad close hoga
        ui.postDelayed(this::closeRecIfDone, 2500);
    }

    private void closeRecIfDone() {
        closeRecStream(true);
        bar.setText("✓ Gallery me save: Movies/LectureSaver " + recName());
    }

    private String recName() {
        String ext = recMime != null && recMime.contains("mp4") ? ".mp4" : ".webm";
        return "lecture-" + recStamp() + ext;
    }

    private static String recStamp() {
        return new java.text.SimpleDateFormat("yyyyMMdd-HHmmss", java.util.Locale.US)
                .format(new java.util.Date());
    }

    /** MediaRecorder chunk -> seedha MediaStore file me (bade lecture ke liye zaroori). */
    private void openRecStream() {
        closeRecStream(false);
        String name = recName();
        String mime = mimeOf(name);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                android.content.ContentValues cv = new android.content.ContentValues();
                cv.put(android.provider.MediaStore.Video.Media.DISPLAY_NAME, name);
                cv.put(android.provider.MediaStore.Video.Media.MIME_TYPE, mime);
                cv.put(android.provider.MediaStore.Video.Media.RELATIVE_PATH,
                        Environment.DIRECTORY_MOVIES + "/LectureSaver");
                cv.put(android.provider.MediaStore.Video.Media.IS_PENDING, 1);
                recUri = getContentResolver().insert(
                        android.provider.MediaStore.Video.Media.EXTERNAL_CONTENT_URI, cv);
                if (recUri == null) return;
                recOut = getContentResolver().openOutputStream(recUri, "w");
            } else {
                File dir = new File(Environment.getExternalStoragePublicDirectory(
                        Environment.DIRECTORY_MOVIES), "LectureSaver");
                if (!dir.exists()) dir.mkdirs();
                File f = new File(dir, name);
                recOut = new java.io.FileOutputStream(f);
            }
        } catch (Exception e) {
            bar.setText("rec file fail: " + e.getMessage());
        }
    }

    private String mimeOf(String name) {
        return name.endsWith(".mp4") ? "video/mp4" : "video/webm";
    }

    private void closeRecStream(boolean ok) {
        try {
            if (recOut != null) {
                recOut.flush();
                recOut.close();
            }
        } catch (Exception ignored) {
        }
        recOut = null;
        if (ok && recUri != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            android.content.ContentValues cv = new android.content.ContentValues();
            cv.put(android.provider.MediaStore.Video.Media.IS_PENDING, 0);
            getContentResolver().update(recUri, cv, null, null);
        }
        recUri = null;
    }

    /** Recorder ka bridge: chunks (base64) + status. */
    public class RecBridge {
        @android.webkit.JavascriptInterface
        public void postMessage(String json) {
            if (json == null) return;
            try {
                org.json.JSONObject o = new org.json.JSONObject(json);
                String type = o.optString("type");
                if ("rec".equals(type)) {
                    final String b64 = o.optString("b64");
                    final String mime = o.optString("mime", "");
                    ui.post(() -> {
                        if (recOut == null) openRecStream();
                        try {
                            byte[] data = android.util.Base64.decode(b64, android.util.Base64.DEFAULT);
                            if (recOut != null) {
                                recOut.write(data);
                                recBytes += data.length;
                            }
                        } catch (Exception e) {
                            bar.setText("rec write fail: " + e.getMessage());
                        }
                    });
                    return;
                }
                if ("rec-done".equals(type)) {
                    ui.post(() -> {
                        if (recording) {
                            recording = false;
                            finishedOnce = true;
                            recBtn.setText("⏺ REC");
                            closeRecStream(true);
                            bar.setText("✓ recording save ho gayi ("
                                    + (recBytes / 1048576.0) + " MB) — Gallery dekhein");
                        }
                    });
                    return;
                }
                if ("log".equals(type)) {
                    final String msg = o.optString("msg");
                    ui.post(() -> bar.setText(msg));
                }
            } catch (Exception ignored) {
            }
        }
    }

    /* ---------------------------------------------------------------- utils -- */

    private Button btn(String text, android.view.View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextSize(10f);
        b.setPadding(8, 0, 8, 0);
        b.setOnClickListener(l);
        b.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                (int) (42 * getResources().getDisplayMetrics().density)));
        return b;
    }

    private final Runnable tickRunnable = this::tick;

    private void tick() {
        int n = NetSniffer.hits().size();
        int k = NetSniffer.keyHits().size();
        if (!downloading) bar.setText("page: " + n + " req · key " + k
                + (BrowserNet.ready() ? " · browser-net ready" : ""));
        ui.postDelayed(tickRunnable, 2000);
    }

    private void injectAsset(String name) {
        try {
            java.io.InputStream in = getAssets().open(name);
            java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
            in.close();
            web.evaluateJavascript(bos.toString("UTF-8"), null);
        } catch (Exception ignored) {
        }
    }

    private static String unquote(String v) {
        if (v == null) return "";
        String s = v.trim();
        if (s.length() > 1 && s.startsWith("\"") && s.endsWith("\"")) {
            try {
                return new org.json.JSONObject("{\"v\":" + s + "}").getString("v");
            } catch (Exception e) {
                return s.substring(1, s.length() - 1);
            }
        }
        return s;
    }

    private static String jsStr(String s) {
        return "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'";
    }

    private void copyLog() {
        StringBuilder sb = new StringBuilder();
        for (NetSniffer.Hit h : NetSniffer.hits()) {
            sb.append(h.host).append(" ").append(h.status).append("  ").append(h.url).append("\n");
            for (java.util.Map.Entry<String, String> e : h.requestHeaders.entrySet()) {
                sb.append("  ").append(e.getKey()).append(": ").append(e.getValue()).append("\n");
            }
            sb.append("\n");
        }
        try {
            ClipboardManager cm = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
            if (cm != null) {
                cm.setPrimaryClip(ClipData.newPlainText("page-log", sb.toString()));
                toast("log copy (" + NetSniffer.hits().size() + " requests)");
            }
        } catch (Exception ignored) {
        }
    }

    private void toast(String m) {
        Toast.makeText(this, m, Toast.LENGTH_SHORT).show();
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        ui.removeCallbacksAndMessages(null);
        closeRecStream(false);
        if (web != null) web.destroy();
        super.onDestroy();
    }

    /** Page ke andar download-catcher: blob URL ko pakad ke app ko bhejo. */
    private static final String DL_CATCHER_JS =
            "(function(){if(window.__lsDlHook)return;window.__lsDlHook=1;"
                    + "var ao=URL.createObjectURL;"
                    + "URL.createObjectURL=function(b){var u=ao.call(URL,b);"
                    + " try{window.__lsLastBlob=u;window.__lsLastMime=b&&b.type||'';}catch(e){}return u;};"
                    + "var a=document.createElement('a');"
                    + "document.addEventListener('click',function(e){"
                    + "  var t=e.target;"
                    + "  while(t&&t.tagName!=='A')t=t.parentElement;"
                    + "  if(!t)return;"
                    + "  var h=t.getAttribute('href')||'';"
                    + "  if(h.indexOf('blob:')===0||t.hasAttribute('download')){"
                    + "    try{window.BrowserNetBridge.chunk(0,'',true,'link:'+h+':'+(t.getAttribute('download')||''));}catch(x){}"
                    + "  }"
                    + "},true);})();";
}
