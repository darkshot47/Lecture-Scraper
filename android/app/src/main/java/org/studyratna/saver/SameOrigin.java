package org.studyratna.saver;

import android.os.Handler;
import android.os.Looper;
import android.webkit.CookieManager;
import android.webkit.WebSettings;
import android.webkit.WebView;

import java.io.ByteArrayOutputStream;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * SameOrigin — WebView ke andar "us origin ka apna page" bana ke fetch karna.
 *
 * KYUN (ye research ka nichod hai):
 *   - native okhttp/jdk  -> media playlist par 502 (transport/edge reject)
 *   - page se fetch()    -> HTTP 0 (CORS; target cross-origin hai)
 *   - WebView khud page  -> 200 (yahi wajah hai ki site par video chalti hai)
 *
 * Jugad: WebView me `loadDataWithBaseURL("https://player.studyratna.org/", html)`
 * load karo. Us document ka **origin wahi host** ban jaata hai, isliye usi host
 * par `fetch()` **same-origin** hai -> CORS lagta hi nahi, aur bytes WebView ke
 * apne network stack se aate hain (wahi stack jise Cloudflare 200 deta hai).
 *
 * Cookie: token ki CloudFront cookie + WebView jar, host ke liye pehle seed kar
 * dete hain, taaki request bilkul browser jaisi ho.
 */
public final class SameOrigin {

    public interface Done {
        void run(int status, byte[] body, String text, String note);
    }

    private static WebView web;
    private static final Handler UI = new Handler(Looper.getMainLooper());

    private static final class Job {
        final Done cb;
        final ByteArrayOutputStream body = new ByteArrayOutputStream();
        int status = 0;
        String note = "";
        boolean finished = false;

        Job(Done cb) {
            this.cb = cb;
        }
    }

    private static Job current;

    private SameOrigin() {
    }

    /** Ek hidden WebView (Activity ke andar, 1x1) attach karo. */
    public static void attach(final WebView wv) {
        web = wv;
        UI.post(() -> {
            if (web == null) return;
            WebSettings s = web.getSettings();
            s.setJavaScriptEnabled(true);
            s.setDomStorageEnabled(true);
            s.setDatabaseEnabled(true);
            s.setAllowFileAccess(false);
            s.setAllowContentAccess(false);
            s.setCacheMode(WebSettings.LOAD_NO_CACHE);
            s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
            CookieManager cm = CookieManager.getInstance();
            cm.setAcceptCookie(true);
            cm.setAcceptThirdPartyCookies(web, true);
            web.addJavascriptInterface(new Bridge(), "SameOriginBridge");
            web.setWebViewClient(new android.webkit.WebViewClient() {
                @Override
                public void onPageFinished(WebView v, String url) {
                    runPending(v);
                }
            });
        });
    }

    private static volatile String pendingJs;

    /** Page load khatam hone par JS chalao (andhe timer ki jagah). */
    private static void runPending(WebView w) {
        String js = pendingJs;
        if (js == null) return;
        pendingJs = null;
        try {
            w.evaluateJavascript(js, null);
        } catch (Throwable t) {
            finish(0, "eval fail: " + t);
        }
    }

    public static boolean ready() {
        return web != null;
    }

    /* ------------------------------------------------------------------ api -- */

    /**
     * target URL ko uske hi origin ke document se fetch karo.
     * Blocking (background thread se bulao).
     */
    private static final Object LOCK = new Object();

    public static Downloader.Resp get(String targetUrl, String cookie, int timeoutMs) {
        synchronized (LOCK) {   // WebView ek hi hai — ek time par ek fetch
            return getLocked(targetUrl, cookie, timeoutMs);
        }
    }

    private static Downloader.Resp getLocked(String targetUrl, String cookie, int timeoutMs) {
        final Blocking box = new Blocking();
        fetch(targetUrl, cookie, (status, body, text, note) -> {
            box.status = status;
            box.body = body;
            box.text = text;
            box.note = note;
            box.done = true;
            synchronized (box) {
                box.notifyAll();
            }
        });
        synchronized (box) {
            if (!box.done) {
                try {
                    box.wait(Math.max(1000, timeoutMs));
                } catch (InterruptedException ignored) {
                }
            }
        }
        Downloader.Resp r = new Downloader.Resp();
        r.status = box.status;
        r.body = box.body == null ? new byte[0] : box.body;
        r.text = box.text == null ? "" : box.text;
        return r;
    }

    private static final class Blocking {
        int status;
        byte[] body;
        String text;
        String note;
        boolean done;
    }

    /** Non-blocking version (UI se). */
    public static void fetch(String targetUrl, String cookie, Done cb) {
        if (web == null) {
            cb.run(0, new byte[0], "", "webview not attached");
            return;
        }
        final String origin = originOf(targetUrl);
        if (origin == null) {
            cb.run(0, new byte[0], "", "bad url");
            return;
        }

        Job j = new Job(cb);
        synchronized (SameOrigin.class) {
            current = j;
        }

        // 1) cookie seed (host ke liye) — CloudFront policy cookie zaroori
        seedCookies(origin, cookie);

        // 2) us origin ka "khaali page" load karo -> same-origin context
        final String html = "<!doctype html><html><head><meta charset='utf-8'></head>"
                + "<body><p id='so'>same-origin fetch ready</p></body></html>";

        UI.post(() -> {
            try {
                WebView w = web;
                if (w == null) {
                    finish(0, "no webview");
                    return;
                }
                w.loadDataWithBaseURL(origin + "/", html, "text/html", "UTF-8", null);
                // base origin load hone ke baad target fetch
                UI.postDelayed(() -> {
                    String js = "(function(){"
                            + "var U=" + jsStr(targetUrl) + ";"
                            + "var CH=32000;"
                            + "function push(u8,last,st){"
                            + "  var s='';for(var i=0;i<u8.length;i++)s+=String.fromCharCode(u8[i]);"
                            + "  try{window.SameOriginBridge.chunk(st,btoa(s),last,'');}catch(e){}"
                            + "}"
                            + "function fail(st,m){try{window.SameOriginBridge.chunk(st,'',true,String(m).slice(0,150));}catch(e){}}"
                            + "try{"
                            + "  fetch(U,{credentials:'include',cache:'no-store',headers:{'Accept':'*/*'}})"
                            + "   .then(function(r){"
                            + "      var ct=(r.headers.get('content-type')||'').toLowerCase();"
                            + "      if(ct.indexOf('text')>=0||ct.indexOf('mpegurl')>=0||U.indexOf('.m3u8')>=0){"
                            + "        return r.text().then(function(t){"
                            + "           var u8=new TextEncoder().encode(t);"
                            + "           for(var o=0;o<u8.length;o+=CH){push(u8.subarray(o,Math.min(o+CH,u8.length)),o+CH>=u8.length,r.status);}"
                            + "           if(!u8.length)push(new Uint8Array(0),true,r.status);"
                            + "        });"
                            + "      }"
                            + "      return r.arrayBuffer().then(function(ab){"
                            + "         var u8=new Uint8Array(ab);"
                            + "         for(var o=0;o<u8.length;o+=CH){push(u8.subarray(o,Math.min(o+CH,u8.length)),o+CH>=u8.length,r.status);}"
                            + "         if(!u8.length)push(new Uint8Array(0),true,r.status);"
                            + "      });"
                            + "   })"
                            + "   .catch(function(e){fail(0,(e&&e.message)||e);});"
                            + "}catch(e){fail(0,e);}"
                            + "})();";
                    pendingJs = js;
                    runPending(w);   // onPageFinished pehle aa gaya to turant
                }, 900);            // warna 900ms baad (fallback)
            } catch (Throwable t) {
                finish(0, "load fail: " + t);
            }
        });
    }

    /* -------------------------------------------------------------- internal -- */

    private static void seedCookies(String origin, String cookie) {
        if (cookie == null || cookie.isEmpty()) return;
        try {
            CookieManager cm = CookieManager.getInstance();
            for (String pair : cookie.split(";")) {
                String p = pair.trim();
                if (p.isEmpty()) continue;
                cm.setCookie(origin, p + "; Path=/");
                // CloudFront cookie .cloudfront.net domain ke liye bhi
                if (p.startsWith("CloudFront-")) {
                    String cdn = Origin.cdnHostFromCookie(cookie);
                    if (cdn != null) cm.setCookie("https://" + cdn, p + "; Path=/");
                }
            }
            cm.flush();
        } catch (Throwable ignored) {
        }
    }

    private static String originOf(String url) {
        try {
            java.net.URL u = new java.net.URL(url);
            return u.getProtocol() + "://" + u.getHost();
        } catch (Exception e) {
            return null;
        }
    }

    private static void finish(int status, String note) {
        Job j = current;
        if (j == null) return;
        synchronized (SameOrigin.class) {
            current = null;
        }
        j.finished = true;
        byte[] raw = j.body.toByteArray();
        j.cb.run(status > 0 ? status : j.status, raw,
                new String(raw, java.nio.charset.StandardCharsets.UTF_8), note);
    }

    public static synchronized void onChunk(int status, String b64, boolean last, String err) {
        Job j = current;
        if (j == null) return;
        if (status > 0) j.status = status;
        if (err != null && !err.isEmpty()) j.note = err;
        if (b64 != null && !b64.isEmpty()) {
            try {
                j.body.write(android.util.Base64.decode(b64, android.util.Base64.DEFAULT));
            } catch (Exception ignored) {
            }
        }
        if (last) {
            synchronized (SameOrigin.class) {
                current = null;
            }
            j.finished = true;
            byte[] raw = j.body.toByteArray();
            int st = j.status;
            // status 0 + TypeError = CORS/failed
            String note = j.note.isEmpty() && st == 0 ? "fetch failed (CORS/network)" : j.note;
            j.cb.run(st, raw, new String(raw, java.nio.charset.StandardCharsets.UTF_8), note);
        }
    }

    private static String jsStr(String s) {
        return "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'";
    }

    /** JS se aane wale chunks. */
    public static class Bridge {
        @android.webkit.JavascriptInterface
        public void chunk(int status, String b64, boolean last, String err) {
            onChunk(status, b64, last, err);
        }
    }

    /** Diagnostics ke liye. */
    public static void log(String s) {
        BrowserNet.log("SO " + s);
    }

    /** Origin ke hisaab se base URL (probe me dikhane ke liye). */
    public static Map<String, String> describe(String url) {
        Map<String, String> m = new LinkedHashMap<>();
        m.put("target", url == null ? "" : url);
        m.put("origin", url == null ? "" : String.valueOf(originOf(url)));
        m.put("ready", String.valueOf(web != null));
        return m;
    }
}
