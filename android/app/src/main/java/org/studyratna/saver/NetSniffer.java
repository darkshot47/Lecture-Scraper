package org.studyratna.saver;

import android.webkit.WebResourceRequest;
import android.webkit.WebView;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * NetSniffer — WebView ke andar hone wali asli requests dekhta hai.
 *
 * KYUN: key endpoint 401 de raha hai, aur humein exactly nahi pata kaunsa
 * header/cookie maang raha hai. Par jab user app me lecture kholta hai, WebView
 * khud player ko load karta hai — tab jo request player bhejta hai usme sahi
 * Referer / Origin / Cookie / Authorization hote hain.
 *
 * Hum wahi headers pakad ke Downloader ko de dete hain ("sniffed" variant).
 * Browser ki cross-origin problem yahan nahi hoti, kyunki request apne aap
 * WebView ke andar ja rahi hai — hum sirf uska record rakh rahe hain.
 *
 * Ise WHOLE app ke liye ek instance rakha jaata hai (static), taaki recorder
 * page aur downloader page dono ka data ek jagah aaye.
 */
public final class NetSniffer {

    /** Sirf itne hosts ki headers rakhte hain (memory + relevance). */
    private static final String[] WANT = {
            "penpencil", "studyratna", "cloudfront", "pw.live"
    };

    public static final class Hit {
        public String host;
        public String url;
        public int status;
        public Map<String, String> requestHeaders = new LinkedHashMap<>();
        public List<String> setCookies = new ArrayList<>();
        public long at;

        public String summary() {
            return host + " " + status + " · " + requestHeaders.size() + " headers"
                    + (setCookies.isEmpty() ? "" : " · " + setCookies.size() + " cookies");
        }
    }

    private static final List<Hit> HITS = new ArrayList<>();
    private static final Map<String, String> COOKIES = new LinkedHashMap<>();
    private static final Map<String, String> LAST_AUTH = new LinkedHashMap<>();

    private NetSniffer() {
    }

    public static void clear() {
        synchronized (HITS) {
            HITS.clear();
        }
        synchronized (COOKIES) {
            COOKIES.clear();
        }
        synchronized (LAST_AUTH) {
            LAST_AUTH.clear();
        }
    }

    static boolean wanted(String url) {
        if (url == null) return false;
        String u = url.toLowerCase();
        for (String w : WANT) {
            if (u.contains(w)) return true;
        }
        return false;
    }

    /** WebViewClient.shouldInterceptRequest se call hota hai (har request). */
    public static void record(WebView view, WebResourceRequest req) {
        if (req == null || !wanted(req.getUrl().toString())) return;
        Hit h = new Hit();
        h.url = req.getUrl().toString();
        h.host = req.getUrl().getHost();
        h.at = System.currentTimeMillis();
        Map<String, String> hd = req.getRequestHeaders();
        if (hd != null) {
            for (Map.Entry<String, String> e : hd.entrySet()) {
                h.requestHeaders.put(e.getKey(), e.getValue());
            }
        }
        // auth-ish headers alag se yaad rakho (download ke waqt trim karne ke liye)
        for (Map.Entry<String, String> e : h.requestHeaders.entrySet()) {
            String k = e.getKey().toLowerCase();
            if (k.equals("authorization") || k.equals("cookie") || k.startsWith("x-")
                    || k.equals("client-id") || k.equals("client-type") || k.equals("client-version")) {
                synchronized (LAST_AUTH) {
                    LAST_AUTH.put(e.getKey(), e.getValue());
                }
            }
        }
        synchronized (HITS) {
            HITS.add(h);
            while (HITS.size() > 120) HITS.remove(0);
        }
    }

    /** WebView ke cookie store se — download me bhejne ke liye. */
    public static void absorbCookiesFrom(String url) {
        try {
            String c = android.webkit.CookieManager.getInstance().getCookie(url);
            if (c == null || c.isEmpty()) return;
            for (String pair : c.split(";")) {
                int i = pair.indexOf('=');
                if (i > 0) {
                    synchronized (COOKIES) {
                        COOKIES.put(pair.substring(0, i).trim(), pair.substring(i + 1).trim());
                    }
                }
            }
        } catch (Exception ignored) {
        }
    }

    /** Player ne jo cookies set ki (Set-Cookie) — webkit store se hi milti hain. */
    public static String cookieHeader(String... urlsForCookies) {
        StringBuilder sb = new StringBuilder();
        for (String u : urlsForCookies) {
            if (u == null) continue;
            absorbCookiesFrom(u);
        }
        synchronized (COOKIES) {
            for (Map.Entry<String, String> e : COOKIES.entrySet()) {
                if (sb.length() > 0) sb.append("; ");
                sb.append(e.getKey()).append("=").append(e.getValue());
            }
        }
        return sb.toString();
    }

    public static Map<String, String> sniffedAuthHeaders() {
        synchronized (LAST_AUTH) {
            return new LinkedHashMap<>(LAST_AUTH);
        }
    }

    public static List<Hit> hits() {
        synchronized (HITS) {
            return new ArrayList<>(HITS);
        }
    }

    /** Key endpoint se jude hits — diagnostics ke liye ("kya player ne key maangi thi?"). */
    public static List<Hit> keyHits() {
        List<Hit> out = new ArrayList<>();
        for (Hit h : hits()) {
            String u = h.url.toLowerCase();
            if (u.contains("get-hls-key") || u.contains("enc.key") || u.contains("hls-key")) out.add(h);
        }
        return out;
    }
}
