package org.studyratna.saver;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import okhttp3.ConnectionSpec;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;
import okhttp3.TlsVersion;

/**
 * Http — teen transport ek jagah, priority order me.
 *
 * Cloudflare 502 ka asli kaaran usually TLS/ALPN fingerprint hota hai:
 *   - HttpURLConnection  -> "Java/17" jaisa TLS stack, ALPN h2 -> Cloudflare block
 *   - OkHttp + HTTP/1.1 + Chrome headers -> brauzer jaisa request -> 200
 *
 * Order:
 *   1. OkHttp (forced HTTP/1.1, Chrome UA + full header set, redirects follow)
 *   2. HttpURLConnection (purana raasta)
 *   3. BrowserNet (WebView ke ANDAR fetch — same-origin/CORS se bachne ke liye
 *      sirf tab jab data HTML page ki origin se aata ho)
 *
 * Har request ka result `lastRoute` me likha jaata hai taaki screen par pata chale
 * kaun sa transport chala — guess na karna pade.
 */
public final class Http {

    public static final class Res {
        public int status;
        public byte[] body = new byte[0];
        public String text = "";
        public String route = "";
        public String error = "";
        public Map<String, String> headers = new LinkedHashMap<>();

        public boolean ok() {
            return status == 200 && body.length > 0;
        }
    }

    public static String lastRoute = "";

    private static OkHttpClient client;

    private Http() {
    }

    private static synchronized OkHttpClient client() {
        if (client == null) {
            client = new OkHttpClient.Builder()
                    .connectTimeout(20, TimeUnit.SECONDS)
                    .readTimeout(40, TimeUnit.SECONDS)
                    .writeTimeout(40, TimeUnit.SECONDS)
                    .followRedirects(true)
                    .followSslRedirects(true)
                    .retryOnConnectionFailure(true)
                    .protocols(java.util.Collections.singletonList(okhttp3.Protocol.HTTP_1_1))
                    .connectionSpecs(java.util.Arrays.asList(
                            // Chrome jaisa TLS: TLS1.3 + 1.2, modern ciphers
                            new ConnectionSpec.Builder(ConnectionSpec.MODERN_TLS)
                                    .tlsVersions(TlsVersion.TLS_1_3, TlsVersion.TLS_1_2)
                                    .build(),
                            ConnectionSpec.COMPATIBLE_TLS,
                            ConnectionSpec.CLEARTEXT))
                    .build();
        }
        return client;
    }

    /* --------------------------------------------------------------- public -- */

    /** Default: OkHttp, phir HttpURLConnection. BrowserNet caller khud kare. */
    public static Res get(String url, String cookie, Map<String, String> extra) {
        Res ok = okhttp(url, cookie, extra, "GET");
        if (ok.status == 200 || ok.status == 206) return ok;
        Res jdk = jdk(url, cookie, extra);
        if (jdk.status == 200 || jdk.status == 206) return jdk;
        // dono fail — jo behtar status de wahi lautao (diagnostics ke liye)
        return ok.status != 0 ? ok : jdk;
    }

    /** OkHttp only (player / segments ke liye). */
    public static Res okhttpOnly(String url, String cookie, Map<String, String> extra) {
        return okhttp(url, cookie, extra, "GET");
    }

    /** HEAD — reachability check (CDN zinda hai ya nahi). */
    public static Res head(String url, String cookie) {
        return okhttp(url, cookie, null, "HEAD");
    }

    /* -------------------------------------------------------------- okhttp -- */

    private static Res okhttp(String url, String cookie, Map<String, String> extra, String method) {
        Res out = new Res();
        out.route = "okhttp";
        try {
            Request.Builder b = new Request.Builder().url(url).method(method, null);

            // --- browser jaisa header set (Cloudflare fingerprint bypass) ---
            b.header("User-Agent", UA);
            b.header("Accept", "*/*");
            b.header("Accept-Language", "en-IN,en;q=0.9,hi;q=0.8,en;q=0.7");
            b.header("Accept-Encoding", "identity");
            String host = Origin.host(url);
            String ref = host.contains("cloudfront") || host.contains("amazonaws")
                    ? "https://player.studyratna.org/" : "https://app.studyratna.org/";
            b.header("Referer", ref);
            b.header("Origin", ref.substring(0, ref.length() - 1));
            b.header("Sec-Fetch-Dest", "empty");
            b.header("Sec-Fetch-Mode", "cors");
            b.header("Sec-Fetch-Site", "cross-site");
            b.header("Sec-CH-UA", "\"Chromium\";v=\"130\", \"Android WebView\";v=\"130\"");
            b.header("Sec-CH-UA-Mobile", "?1");
            b.header("Sec-CH-UA-Platform", "\"Android\"");

            if (cookie != null && !cookie.isEmpty()) b.header("Cookie", cookie);
            if (extra != null) {
                for (Map.Entry<String, String> e : extra.entrySet()) {
                    if (e.getKey() == null || e.getValue() == null) continue;
                    if (e.getKey().startsWith("X-")) continue;
                    if (e.getKey().equalsIgnoreCase("Host")) continue;
                    b.header(e.getKey(), e.getValue());
                }
            }

            Response r = client().newCall(b.build()).execute();
            out.status = r.code();
            for (String n : r.headers().names()) out.headers.put(n, r.header(n));
            ResponseBody body = r.body();
            out.body = body == null ? new byte[0] : body.bytes();
            out.text = new String(out.body, java.nio.charset.StandardCharsets.UTF_8);
            r.close();
            lastRoute = "okhttp/" + host + "/" + out.status;
        } catch (Throwable e) {
            out.status = 0;
            out.error = e.getMessage() == null ? e.toString() : e.getMessage();
            lastRoute = "okhttp/FAIL/" + out.error;
        }
        return out;
    }

    /* ----------------------------------------------------------------- jdk -- */

    private static Res jdk(String url, String cookie, Map<String, String> extra) {
        Res out = new Res();
        out.route = "jdk";
        java.net.HttpURLConnection c = null;
        try {
            c = (java.net.HttpURLConnection) new java.net.URL(url).openConnection();
            c.setConnectTimeout(20000);
            c.setReadTimeout(30000);
            c.setInstanceFollowRedirects(true);
            c.setRequestProperty("User-Agent", UA);
            c.setRequestProperty("Accept", "*/*");
            c.setRequestProperty("Accept-Encoding", "identity");
            c.setRequestProperty("Referer", "https://app.studyratna.org/");
            if (cookie != null && !cookie.isEmpty()) c.setRequestProperty("Cookie", cookie);
            if (extra != null) {
                for (Map.Entry<String, String> e : extra.entrySet()) {
                    if (e.getKey() == null || e.getKey().startsWith("X-")) continue;
                    c.setRequestProperty(e.getKey(), e.getValue());
                }
            }
            out.status = c.getResponseCode();
            java.io.InputStream in = out.status >= 400 ? c.getErrorStream() : c.getInputStream();
            out.body = in == null ? new byte[0] : Downloader.readAll(in);
            out.text = new String(out.body, java.nio.charset.StandardCharsets.UTF_8);
            lastRoute = "jdk/" + Origin.host(url) + "/" + out.status;
        } catch (Throwable e) {
            out.status = 0;
            out.error = e.getMessage() == null ? e.toString() : e.getMessage();
            lastRoute = "jdk/FAIL/" + out.error;
        } finally {
            if (c != null) c.disconnect();
        }
        return out;
    }

    /** Chrome jaisa UA — app ka naam nahi (fingerprint na bane). */
    static final String CHROME_UA =
            "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) "
                    + "Chrome/130.0.0.0 Mobile Safari/537.36";

    public static final String UA = CHROME_UA;
}
