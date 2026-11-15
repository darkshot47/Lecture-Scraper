package org.studyratna.saver;

import android.os.Handler;
import android.os.Looper;
import android.webkit.WebView;

import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * BrowserNet — requests ko WebView ke ANDAR bhejta hai aur bytes wapas laata hai.
 *
 * KYUN: native HttpURLConnection Cloudflare se 502 khaata hai (IP/TLS fingerprint
 * block). Par wahi URL jab WebView ke andar fetch() se maanga jaata hai — wahi
 * context jahan user ka player chal raha hai, wahi cookies, wahi headers — to
 * 200 aata hai. Isliye "browser mode" yahan se hota hai.
 *
 * Kaam kaise:
 *   1. hidden WebView me JS inject karo (page ke same origin se fetch)
 *   2. response ko 48 KB chunks me base64 karke AndroidBridge par wapas bhejo
 *   3. caller ko bytes de do (HLS playlist, segments, key — sab)
 */
public final class BrowserNet {

    public interface Done {
        void run(int status, byte[] body, String text);
    }

    private static WebView web;
    private static final Handler UI = new Handler(Looper.getMainLooper());

    /** Ek hi WebView reuse hota hai (cookies + TLS session bacha rehta hai). */
    public static void attach(final WebView wv) {
        web = wv;
        UI.post(() -> {
            if (web == null) return;
            web.getSettings().setJavaScriptEnabled(true);
            web.getSettings().setDomStorageEnabled(true);
            web.getSettings().setAllowFileAccess(false);
            android.webkit.CookieManager.getInstance().setAcceptCookie(true);
            android.webkit.CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);
            web.addJavascriptInterface(new Bridge(), "BrowserNetBridge");
        });
    }

    public static boolean ready() {
        return web != null;
    }

    /** JS ko bhejo (main thread par). */
    public static void eval(final String js) {
        UI.post(() -> {
            if (web == null) return;
            try {
                web.evaluateJavascript(js, null);
            } catch (Exception ignored) {
            }
        });
    }

    /* ------------------------------------------------------------------ state -- */

    private static final class Job {
        final String url;
        final Done cb;
        final ByteArrayOutputStream body = new ByteArrayOutputStream();
        int status = 0;
        String error = "";
        boolean done = false;

        Job(String url, Done cb) {
            this.url = url;
            this.cb = cb;
        }
    }

    private static Job current;

    /** Chunk hazaar me aata hai; 48KB base64 ~ 36KB raw. */
    public static synchronized void onChunk(int status, String b64, boolean last, String err) {
        Job j = current;
        if (j == null) return;
        if (status > 0) j.status = status;
        if (err != null && !err.isEmpty()) j.error = err;
        if (b64 != null && !b64.isEmpty()) {
            try {
                j.body.write(android.util.Base64.decode(b64, android.util.Base64.DEFAULT));
            } catch (Exception ignored) {
            }
        }
        if (last) {
            j.done = true;
            byte[] raw = j.body.toByteArray();
            String text = new String(raw, java.nio.charset.StandardCharsets.UTF_8);
            current = null;
            j.cb.run(j.status, raw, text);
        }
    }

    /* ------------------------------------------------------------------- api -- */

    /** WebView ke andar se fetch — bytes chahiye (key / segment / playlist). */
    public static void fetchRaw(String url, Map<String, String> headers, Done cb) {
        start(url, headers, "array", cb);
    }

    /** WebView ke andar se fetch — text chahiye (m3u8). */
    public static void fetchText(String url, Map<String, String> headers, Done cb) {
        start(url, headers, "text", cb);
    }

    /** Blocking wrapper — Downloader background thread se use karta hai. */
    public static Resp getText(String url) {
        final Resp out = new Resp();
        final Object lock = new Object();
        fetchText(url, null, (status, body, text) -> {
            out.status = status;
            out.text = text;
            out.body = body;
            synchronized (lock) {
                lock.notifyAll();
            }
        });
        synchronized (lock) {
            try {
                lock.wait(25000);
            } catch (InterruptedException ignored) {
            }
        }
        return out;
    }

    public static class Resp {
        public int status;
        public byte[] body = new byte[0];
        public String text = "";
    }

    /* -------------------------------------------------------------- internals -- */

    private static void start(String url, Map<String, String> headers, String mode, Done cb) {
        if (web == null) {
            cb.run(0, new byte[0], "");
            return;
        }
        Job j = new Job(url, cb);
        synchronized (BrowserNet.class) {
            current = j;
        }
        String hdr = "{}";
        if (headers != null && !headers.isEmpty()) {
            StringBuilder sb = new StringBuilder("{");
            for (Map.Entry<String, String> e : headers.entrySet()) {
                if (e.getKey().startsWith("X-")) continue;
                if (sb.length() > 1) sb.append(",");
                sb.append(jsStr(e.getKey())).append(":").append(jsStr(e.getValue()));
            }
            hdr = sb.append("}").toString();
        }
        final String js = "(function(){"
                + "var U=" + jsStr(url) + ",H=" + hdr + ",MODE=" + jsStr(mode) + ";"
                + "var CH=33000;"
                + "function chunk(u8,last,stat){"
                + "  var s='';for(var i=0;i<u8.length;i++)s+=String.fromCharCode(u8[i]);"
                + "  try{window.BrowserNetBridge.chunk(stat,btoa(s),last,'');}catch(e){}"
                + "}"
                + "function fail(st,msg){try{window.BrowserNetBridge.chunk(st,'',true,String(msg).slice(0,200));}catch(e){}}"
                + "try{"
                + " fetch(U,{credentials:'include',headers:H,referrer:location.href,mode:'cors'})"
                + " .then(function(r){"
                + "   if(MODE==='array'){"
                + "     return r.arrayBuffer().then(function(ab){"
                + "       var u8=new Uint8Array(ab);"
                + "       for(var o=0;o<u8.length;o+=CH){chunk(u8.subarray(o,Math.min(o+CH,u8.length)),o+CH>=u8.length,r.status);}"
                + "       if(!u8.length)chunk(new Uint8Array(0),true,r.status);"
                + "     });"
                + "   }else{"
                + "     return r.text().then(function(t){"
                + "       var u8=new TextEncoder().encode(t);"
                + "       for(var o=0;o<u8.length;o+=CH){chunk(u8.subarray(o,Math.min(o+CH,u8.length)),o+CH>=u8.length,r.status);}"
                + "       if(!u8.length)chunk(new Uint8Array(0),true,r.status);"
                + "     });"
                + "   }"
                + " }).catch(function(e){fail(0,e&&e.message?e.message:e);});"
                + "}catch(e){fail(0,e);}"
                + "})();";
        eval(js);
    }

    private static String jsStr(String s) {
        if (s == null) return "''";
        return "'" + s.replace("\\\\", "\\\\\\\\").replace("'", "\\\\'")
                .replace("\\n", "\\\\n").replace("\\r", "") + "'";
    }

    /**
     * Ek JS chalाओ aur uske chunk() calls ko collect karo.
     * (blob: download, data: download — JS se bytes nikaalne ke liye.)
     */
    public static final class BridgeCollect {
        public interface Done {
            void run(int status, byte[] body);
        }

        private static Job collectJob;

        public static void collect(String js, Done cb) {
            Job j = new Job("collect", (status, body, text) -> cb.run(status, body));
            collectJob = j;
            // collectJob ko chunk() route karega
            synchronized (BrowserNet.class) {
                current = j;
            }
            eval(js);
        }
    }

    /** JS se aane wale chunks (page ke andar se). */
    public static class Bridge {
        @android.webkit.JavascriptInterface
        public void chunk(int status, String b64, boolean last, String err) {
            onChunk(status, b64, last, err);
        }
    }

    /** Diagnostics: jo bhi fetch chala, uska record. */
    private static final List<String> LOG = new ArrayList<>();

    public static void log(String s) {
        synchronized (LOG) {
            LOG.add(s);
            while (LOG.size() > 40) LOG.remove(0);
        }
    }

    public static List<String> logLines() {
        synchronized (LOG) {
            return new ArrayList<>(LOG);
        }
    }
}
