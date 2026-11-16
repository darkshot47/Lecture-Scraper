package org.studyratna.saver;

import android.os.Handler;
import android.os.Looper;
import android.webkit.CookieManager;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Map;

/**
 * DocFetch — "document navigation" transport.
 *
 * KYUN (probe table se, guess nahi):
 *   master player okhttp  -> 200
 *   master player sameorg -> 502   <-- fetch() WebView ke andar se BHI 502
 *   media  player okhttp  -> 502 (6458 B error page)
 *
 * Matlab 502 transport ka nahi, **request ke roop** ka hai. fetch()/XHR aur
 * native client dono ek hi tarah dikhte hain (mode=cors, apikey header, wahi
 * Accept). Asli page me video chal rahi hai — yaani jo cheez kaam karti hai wo
 * hai **document navigation** (top-level GET jo site ne khud kiya).
 *
 * Isliye yahan request WebView se **navigation** karwate hain, aur response
 * `shouldInterceptRequest` me pakad lete hain (us waqt hum khud asli bytes
 * laate hain — cookies WebView ke apne jar se, isliye signed cookie bhi sahi).
 *
 * Isse 3 cheezein milti hain:
 *   1. Cloudflare ke liye request ek page-load hai, API call nahi -> 502 nahi
 *   2. CloudFront 403 ka kaaran galat host tha; host ab cookie se aata hai
 *   3. Content-Type / status / body sab asli milta hai (guess nahi)
 */
public final class DocFetch {

    /** Ek request ka natija. */
    public static final class Res {
        public int status;
        public byte[] body = new byte[0];
        public String text = "";
        public String url = "";
        public String note = "";
        public Map<String, String> headers;

        public boolean ok() {
            return status == 200 && body.length > 0;
        }
    }

    private static WebView web;
    private static final Handler UI = new Handler(Looper.getMainLooper());
    private static final Object LOCK = new Object();

    private static volatile String target;      // jo URL intercept karna hai
    private static volatile Res pending;        // natija yahan
    private static volatile boolean done;

    private DocFetch() {
    }

    public static void attach(final WebView wv) {
        web = wv;
        UI.post(() -> {
            if (web == null) return;
            WebSettings s = web.getSettings();
            s.setJavaScriptEnabled(true);
            s.setDomStorageEnabled(true);
            s.setCacheMode(WebSettings.LOAD_NO_CACHE);
            s.setUserAgentString(Http.UA);
            if (android.os.Build.VERSION.SDK_INT >= 21) {
                s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
            }
            CookieManager cm = CookieManager.getInstance();
            cm.setAcceptCookie(true);
            cm.setAcceptThirdPartyCookies(web, true);
            web.setWebViewClient(new WebViewClient() {
                @Override
                public WebResourceResponse shouldInterceptRequest(WebView v, WebResourceRequest req) {
                    try {
                        String u = req.getUrl().toString();
                        String t = target;
                        if (t != null && u.equals(t)) {
                            Res r = fetchNative(u);
                            synchronized (LOCK) {
                                pending = r;
                                done = true;
                                LOCK.notifyAll();
                            }
                            return new WebResourceResponse(
                                    mime(req, r),
                                    null,
                                    200,
                                    "OK",
                                    new java.util.HashMap<>(),
                                    new java.io.ByteArrayInputStream(r.body));
                        }
                    } catch (Throwable ignored) {
                    }
                    return null;
                }

                @Override
                public void onPageFinished(WebView v, String url) {
                    // intercept na hua ho to bhi job khatam karo (hanging na ho)
                    new Thread(() -> {
                        try {
                            Thread.sleep(600);
                        } catch (InterruptedException ignored) {
                        }
                        synchronized (LOCK) {
                            if (!done) {
                                done = true;
                                LOCK.notifyAll();
                            }
                        }
                    }, "docfetch-timeout").start();
                }
            });
        });
    }

    public static boolean ready() {
        return web != null;
    }

    /** Blocking: background thread se bulao. */
    public static Res get(String url, String cookie, int timeoutMs) {
        Res r = new Res();
        r.url = url;
        if (web == null) {
            r.note = "webview not attached";
            return r;
        }
        synchronized (LOCK) {
            target = url;
            pending = null;
            done = false;
        }
        UI.post(() -> {
            try {
                // cookie jar seed karo (asli CloudFront/Cloudflare cookies)
                seedCookies(url, cookie);
                web.loadUrl(url);
            } catch (Throwable t) {
                synchronized (LOCK) {
                    Res e = new Res();
                    e.url = url;
                    e.note = "load fail: " + t;
                    pending = e;
                    done = true;
                    LOCK.notifyAll();
                }
            }
        });
        synchronized (LOCK) {
            long end = System.currentTimeMillis() + Math.max(3000, timeoutMs);
            while (!done) {
                long left = end - System.currentTimeMillis();
                if (left <= 0) break;
                try {
                    LOCK.wait(Math.min(left, 1500));
                } catch (InterruptedException ignored) {
                }
            }
            Res out = pending;
            target = null;
            if (out == null) {
                out = new Res();
                out.url = url;
                out.note = "timeout";
            }
            return out;
        }
    }

    /* ---------------------------------------------------------------- internal */

    private static String mime(WebResourceRequest req, Res r) {
        String p = req.getUrl().getPath();
        if (p != null && p.endsWith(".m3u8")) return "application/vnd.apple.mpegurl";
        String ct = r.headers == null ? null : r.headers.get("content-type");
        if (ct != null && ct.contains("mpegurl")) return "application/vnd.apple.mpegurl";
        return "application/octet-stream";
    }

    /**
     * Asli bytes native se — par cookies WebView ke jar se (jisme site ne khud
     * set ki hui signed cookies hoti hain).
     */
    private static Res fetchNative(String url) {
        Res r = new Res();
        r.url = url;
        try {
            String jar = CookieManager.getInstance().getCookie(url);
            Http.Res h = Http.okhttpOnly(url, jar == null ? "" : jar, null);
            r.status = h.status;
            r.body = h.body == null ? new byte[0] : h.body;
            r.text = h.text == null ? "" : h.text;
            r.headers = h.headers;
            if (!h.ok() && r.body.length == 0) {
                // okhttp ne kuch nahi diya -> JDK raasta (dono transport try)
                Res j = jdk(url, jar == null ? "" : jar);
                if (j.status != 0) {
                    r = j;
                    r.url = url;
                }
            }
        } catch (Throwable t) {
            r.note = String.valueOf(t);
        }
        return r;
    }

    private static Res jdk(String url, String cookie) {
        Res out = new Res();
        out.url = url;
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setInstanceFollowRedirects(true);
            c.setConnectTimeout(15000);
            c.setReadTimeout(25000);
            c.setRequestProperty("User-Agent", Http.UA);
            c.setRequestProperty("Accept", "*/*");
            c.setRequestProperty("Referer", "https://app.studyratna.org/");
            if (cookie != null && !cookie.isEmpty()) c.setRequestProperty("Cookie", cookie);
            out.status = c.getResponseCode();
            InputStream in = out.status >= 400 ? c.getErrorStream() : c.getInputStream();
            if (in != null) {
                ByteArrayOutputStream bo = new ByteArrayOutputStream();
                byte[] buf = new byte[16384];
                int n;
                while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
                in.close();
                out.body = bo.toByteArray();
                out.text = new String(out.body, "UTF-8");
            }
        } catch (Throwable t) {
            out.note = String.valueOf(t);
        } finally {
            if (c != null) c.disconnect();
        }
        return out;
    }

    private static void seedCookies(String url, String cookie) {
        if (cookie == null || cookie.isEmpty()) return;
        try {
            CookieManager cm = CookieManager.getInstance();
            String origin = originOf(url);
            for (String pair : cookie.split(";")) {
                String p = pair.trim();
                if (p.isEmpty()) continue;
                if (origin != null) cm.setCookie(origin, p + "; Path=/");
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
            URL u = new URL(url);
            return u.getProtocol() + "://" + u.getHost();
        } catch (Exception e) {
            return null;
        }
    }
}
