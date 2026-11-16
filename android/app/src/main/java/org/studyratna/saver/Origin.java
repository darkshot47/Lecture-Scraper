package org.studyratna.saver;

import android.webkit.CookieManager;

import java.net.URL;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * Origin — "kaun sa host actually kaam karta hai" ka faisla.
 *
 * RESEARCH (is repo ke purane debug reports se + live behaviour):
 *   - player.studyratna.org  -> Cloudflare -> datacenter/native request par 502
 *   - d1d34p8vz63oiq.cloudfront.net -> CDN origin, cookie (CloudFront-Policy /
 *     Signature / Key-Pair-Id) ke saath **200** deta hai, Cloudflare beech me nahi
 *   - api.penpencil.co (key) -> Cloudflare -> native par 403/401
 *
 * Isliye yahan teen kaam hote hain:
 *   1. token ke videoUrl se CloudFront (ya doosra CDN) URL banao
 *   2. cookie jar: token cookie + WebView ke asli cookies (yahi missing tha)
 *   3. cookie ko "jar" ki tarah rakho taaki CDN jo nayi cookie de wo bhi jaye
 */
public final class Origin {

    /** Known CDN hosts (iske alawa bhi generic detect hota hai). */
    private static final String[] CDN_HOSTS = {
            "cloudfront.net",
            "akamaized.net",
            "cloudflarestream.com",
            "b-cdn.net",
            "fastly.net",
            "azureedge.net",
            "pw.live",
            "penpencil.co"
    };

    private Origin() {
    }

    /* ------------------------------------------------------------- origin -- */

    public static boolean isCdn(String url) {
        if (url == null) return false;
        String h = host(url);
        for (String c : CDN_HOSTS) {
            if (h.endsWith(c) || h.contains(c)) return true;
        }
        return false;
    }

    public static String host(String url) {
        try {
            return new URL(url).getHost().toLowerCase(Locale.US);
        } catch (Exception e) {
            return "";
        }
    }

    /**
     * token ka videoUrl -> CDN par seedha URL.
     * Shape: https://player.studyratna.org/stream/<videoKey>/master.m3u8
     *   ->   https://d1d34p8vz63oiq.cloudfront.net/<videoKey>/master.m3u8
     */
    public static String cdnUrlFrom(String videoUrl, String cookie) {
        String base = cdnBase(cookie);
        String u = cdnUrl(videoUrl);
        if (u == null) return null;
        try {
            java.net.URL a = new java.net.URL(u);
            java.net.URL b = new java.net.URL(base);
            return base + a.getPath() + (a.getQuery() == null ? "" : "?" + a.getQuery());
        } catch (Exception e) {
            return u;
        }
    }

    public static String cdnUrl(String videoUrl) {
        try {
            URL u = new URL(videoUrl);
            String path = u.getPath();                 // /stream/<key>/master.m3u8
            String[] parts = path.split("/");
            String key = null;
            for (int i = 0; i < parts.length; i++) {
                if (parts[i].equals("stream") && i + 1 < parts.length) {
                    key = parts[i + 1];
                    break;
                }
            }
            if (key == null) {
                for (String p : parts) {
                    if (p.matches("[0-9a-fA-F-]{32,40}")) {
                        key = p;
                        break;
                    }
                }
            }
            if (key == null) return null;
            String rest = videoUrl.substring(videoUrl.indexOf(key) + key.length());
            if (rest.startsWith("/")) rest = rest.substring(1);
            return "https://d1d34p8vz63oiq.cloudfront.net/" + key + "/" + rest;
        } catch (Exception e) {
            return null;
        }
    }

    /**
     * Playlist / segment URL ko CDN par le jao (agar host Cloudflare hai).
     * Agar already CDN par hai to waisa hi.
     */
    public static String toCdnOn(String url, String videoKey, String base) {
        if (url == null) return null;
        if (base == null) return toCdn(url, videoKey);
        try {
            java.net.URL u = new java.net.URL(url);
            String h = u.getHost().toLowerCase(Locale.US);
            if (h.contains("cloudfront")) {
                // host badalna hai (cookie kis host ke liye signed hai)
                return base + u.getPath() + (u.getQuery() == null ? "" : "?" + u.getQuery());
            }
            if (!h.contains("studyratna") && !h.contains("penpencil")) return url;
            if (videoKey == null) return url;
            String path = u.getPath();
            int i = path.indexOf("stream/" + videoKey);
            if (i >= 0) path = path.substring(i + ("stream/" + videoKey).length());
            if (path.startsWith("/")) path = path.substring(1);
            return base + "/" + videoKey + "/" + path;
        } catch (Exception e) {
            return url;
        }
    }

    public static String toCdn(String url, String videoKey) {
        if (url == null) return null;
        if (isCdn(url)) return url;
        try {
            URL u = new URL(url);
            String h = u.getHost().toLowerCase(Locale.US);
            if (!h.contains("studyratna") && !h.contains("penpencil")) return url;
            if (videoKey == null) return url;
            String path = u.getPath();
            int i = path.indexOf("stream/" + videoKey);
            if (i >= 0) {
                path = path.substring(i + ("stream/" + videoKey).length());
            }
            if (path.startsWith("/")) path = path.substring(1);
            return "https://d1d34p8vz63oiq.cloudfront.net/" + videoKey + "/" + path;
        } catch (Exception e) {
            return url;
        }
    }

    /** videoKey nikaalo (token URL se). */
    public static String videoKey(String videoUrl) {
        try {
            String path = new URL(videoUrl).getPath();
            for (String p : path.split("/")) {
                if (p.matches("[0-9a-fA-F-]{32,40}")) return p;
            }
        } catch (Exception ignored) {
        }
        return null;
    }

    /**
     * CloudFront cookie ke andar asli resource ka host hota hai:
     *   CloudFront-Policy -> base64 JSON {"Statement":[{"Resource":"https://d111....cloudfront.net/..."}]}
     * Isse **exact CDN host** nikalta hai — guess karne ki zaroorat nahi.
     */
    public static String cdnHostFromCookie(String cookie) {
        if (cookie == null) return null;
        try {
            for (String pair : cookie.split(";")) {
                String p = pair.trim();
                int i = p.indexOf('=');
                if (i < 0) continue;
                String name = p.substring(0, i).trim();
                String val = p.substring(i + 1).trim();
                if (!name.startsWith("CloudFront-")) continue;
                // value URL-encoded base64 JSON hoti hai
                String decoded = null;
                try {
                    decoded = new String(java.util.Base64.decode(
                            android.net.Uri.decode(val), java.util.Base64.DEFAULT),
                            java.nio.charset.StandardCharsets.UTF_8);
                } catch (Exception e) {
                    decoded = val;
                }
                java.util.regex.Matcher m = java.util.regex.Pattern
                        .compile("https?://([a-z0-9.-]+cloudfront\\.net)").matcher(decoded);
                if (m.find()) return m.group(1);
                // kuch policies me sirf domain
                java.util.regex.Matcher m2 = java.util.regex.Pattern
                        .compile("([a-z0-9-]+\\.cloudfront\\.net)").matcher(val);
                if (m2.find()) return m2.group(1);
            }
        } catch (Throwable ignored) {
        }
        return null;
    }

    /** Cookie se mila CDN host, warna default. */
    public static String cdnBase(String cookie) {
        String h = cdnHostFromCookie(cookie);
        return h == null ? "https://d1d34p8vz63oiq.cloudfront.net" : "https://" + h;
    }

    /** Sab CDN hosts jo try karne hain (fallback ke liye). */
    public static List<String> cdnBases() {
        List<String> out = new ArrayList<>();
        out.add("https://d1d34p8vz63oiq.cloudfront.net");
        return out;
    }

    /* -------------------------------------------------------------- cookie -- */

    /**
     * Poora cookie header banao:
     *   1. token ki cookie (CloudFront-Policy/Signature/Key-Pair-Id) — mandatory
     *   2. WebView ke asli cookies (jo player ne set ki)
     * Duplicate names hata diye jaate hain, token wali jeetta hai.
     */
    public static String cookieFor(String tokenCookie, String... urls) {
        Set<String> order = new LinkedHashSet<>();
        StringBuilder sb = new StringBuilder();

        for (String pair : split(tokenCookie)) {
            order.add(name(pair));
            if (sb.length() > 0) sb.append("; ");
            sb.append(pair.trim());
        }

        for (String u : urls) {
            if (u == null) continue;
            String c = cookiesForUrl(u);
            for (String pair : split(c)) {
                if (name(pair).isEmpty() || order.contains(name(pair))) continue;
                order.add(name(pair));
                if (sb.length() > 0) sb.append("; ");
                sb.append(pair.trim());
            }
        }
        return sb.toString();
    }

    public static String cookiesForUrl(String url) {
        try {
            String c = CookieManager.getInstance().getCookie(url);
            return c == null ? "" : c;
        } catch (Exception e) {
            return "";
        }
    }

    private static String[] split(String cookie) {
        if (cookie == null || cookie.isEmpty()) return new String[0];
        return cookie.split(";");
    }

    private static String name(String pair) {
        int i = pair.indexOf('=');
        return i > 0 ? pair.substring(0, i).trim() : "";
    }

    /** CloudFront cookie hai? (decrypt ke liye zaroori) */
    public static boolean hasCloudFrontCookie(String cookie) {
        return cookie != null && cookie.contains("CloudFront-");
    }
}
