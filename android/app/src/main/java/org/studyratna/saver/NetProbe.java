package org.studyratna.saver;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * NetProbe — "kaun sa raasta 200 deta hai?" ka seedha jawab.
 *
 * Har URL (master / media / segment / key) ko 4 tarah se maangta hai:
 *   A. OkHttp (HTTP/1.1 + Chrome headers)
 *   B. HttpURLConnection
 *   C. WebView ke andar fetch (page origin)
 *   Ring  : host variations — player.studyratna.org vs CDN (cloudfront) vs path forms
 *
 * Output ek chhoti table: `URL variant transport -> HTTP nnn`. Isse guess khatam.
 */
public final class NetProbe {

    public static final class Row {
        public String what;      // "master" / "media" / "seg0" / "key"
        public String variant;   // "player" / "cdn" / "path"
        public String transport; // "okhttp" / "jdk" / "webview"
        public int status;
        public int bytes;
        public String note = "";
        public String preview = "";   // body ka nichod — asli wajah yahan hoti hai
        public String server = "";    // server / cf-ray — edge ya origin kaun hai

        public String line() {
            if (preview != null && !preview.isEmpty()) {
                return String.format(java.util.Locale.US, "%-6s %-9s %-8s -> %3d  %6d B %s %s%n        | %s",
                        what, variant, transport, status, bytes, note, server, preview);
            }
            return String.format(java.util.Locale.US, "%-6s %-9s %-8s -> %3d  %6d B %s",
                    what, variant, transport, status, bytes, note);
        }
    }

    public interface Sink {
        void onRow(Row r);

        void onDone(List<Row> rows);
    }

    private NetProbe() {
    }

    public static List<Row> runQuick(Token t, String cookie, Sink sink) {
        List<Row> out = new ArrayList<>();

        String videoKey = Origin.videoKey(t.videoUrl);
        String cdn = Origin.cdnUrl(t.videoUrl);
        String jar = Origin.cookieFor(cookie, t.videoUrl, cdn);

        Map<String, String> urls = new LinkedHashMap<>();
        urls.put("player", t.videoUrl);
        if (cdn != null) urls.put("cdn", cdn);

        // --- master playlist ---
        for (Map.Entry<String, String> u : urls.entrySet()) {
            add(out, sink, "master", u.getKey(), "okhttp", Http.okhttpOnly(u.getValue(), jar, null));
            add(out, sink, "master", u.getKey(), "jdk", Http.get(u.getValue(), jar, null));
            if (SameOrigin.ready()) {
                add(out, sink, "master", u.getKey(), "sameorg", Http.sameOriginOnly(u.getValue(), jar));
            }
            if (DocFetch.ready()) {
                DocFetch.Res d = DocFetch.get(u.getValue(), jar, 25000);
                addDoc(out, sink, "master", u.getKey(), d);
            }
        }

        // --- media playlist + segment + key (jo master se mile) ---
        String masterText = null;
        for (Map.Entry<String, String> u : urls.entrySet()) {
            Http.Res r = Http.okhttpOnly(u.getValue(), jar, null);
            if (r.status == 200 && r.text.contains("#EXTM3U")) {
                masterText = r.text;
                break;
            }
        }

        if (masterText != null) {
            String base = urls.containsKey("cdn") && masterText.contains("hls/")
                    ? (cdn != null ? cdn : t.videoUrl) : t.videoUrl;
            // variant
            String variantRel = null;
            String[] lines = masterText.replace("\r", "").split("\n");
            for (int i = 0; i < lines.length; i++) {
                if (lines[i].trim().startsWith("#EXT-X-STREAM-INF:") && i + 1 < lines.length) {
                    String n = lines[i + 1].trim();
                    if (!n.isEmpty() && !n.startsWith("#")) variantRel = n;
                }
            }
            if (variantRel != null) {
                String mediaPlayer = abs(t.videoUrl, variantRel);

                // saare qualities (720/480/360) — shayad sirf ek hi 502 de
                List<String> allVariants = new ArrayList<>();
                for (int i = 0; i < lines.length; i++) {
                    if (lines[i].trim().startsWith("#EXT-X-STREAM-INF:") && i + 1 < lines.length) {
                        String n = lines[i + 1].trim();
                        if (!n.isEmpty() && !n.startsWith("#")) allVariants.add(abs(t.videoUrl, n));
                    }
                }
                for (String v : (allVariants.isEmpty()
                        ? java.util.Collections.singletonList(mediaPlayer) : allVariants)) {
                    String q = v.contains("/hls/") ? v.substring(v.lastIndexOf("/hls/") + 5) : v;
                    int slash = q.indexOf('/');
                    if (slash > 0) q = q.substring(0, slash);
                    add(out, sink, "media", q, "sameorg", Http.sameOriginOnly(v, jar));
                    addDoc(out, sink, "media", q, DocFetch.get(v, jar, 25000));
                    add(out, sink, "media", q, "okhttp", Http.okhttpOnly(v, jar, null));
                }

                String mediaCdn = videoKey == null ? null : Origin.toCdn(mediaPlayer, videoKey);
                if (mediaCdn != null && !mediaCdn.equals(mediaPlayer)) {
                    add(out, sink, "media", "cdn", "okhttp", Http.okhttpOnly(mediaCdn, jar, null));
                }

                // ek media playlist se seg0 + key
                Http.Res mp = Http.okhttpOnly(mediaPlayer, jar, null);
                if (mp.status != 200) {
                    DocFetch.Res md = DocFetch.get(mediaPlayer, jar, 25000);
                    if (md.ok()) {
                        mp = new Http.Res();
                        mp.status = md.status;
                        mp.body = md.body;
                        mp.text = md.text;
                    }
                }
                if (mp.status == 200) {
                    String mediaBase = (mediaCdn != null && mp.text != null) ? mediaCdn : mediaPlayer;
                    String keyUri = null;
                    String firstSeg = null;
                    for (String raw : mp.text.replace("\r", "").split("\n")) {
                        String l = raw.trim();
                        if (l.startsWith("#EXT-X-KEY:")) {
                            java.util.regex.Matcher m =
                                    java.util.regex.Pattern.compile("URI=\"([^\"]+)\"").matcher(l);
                            if (m.find()) keyUri = abs(mediaBase, m.group(1));
                        } else if (!l.isEmpty() && !l.startsWith("#") && firstSeg == null) {
                            firstSeg = abs(mediaBase, l);
                        }
                    }
                    if (firstSeg != null) {
                        add(out, sink, "seg0", "playlist", "okhttp", Http.okhttpOnly(firstSeg, jar, null));
                        if (videoKey != null) {
                            String cdnSeg = Origin.toCdn(firstSeg, videoKey);
                            if (cdnSeg != null && !cdnSeg.equals(firstSeg)) {
                                add(out, sink, "seg0", "cdn", "okhttp", Http.okhttpOnly(cdnSeg, jar, null));
                            }
                        }
                    }
                    if (keyUri != null) {
                        add(out, sink, "key", "as-is", "okhttp", Http.okhttpOnly(keyUri, jar, null));
                        add(out, sink, "key", "as-is", "jdk", Http.get(keyUri, jar, null));
                        if (videoKey != null) {
                            String cdnKey = "https://d1d34p8vz63oiq.cloudfront.net/" + videoKey + "/enc.key";
                            add(out, sink, "key", "cdn", "okhttp", Http.okhttpOnly(cdnKey, jar, null));
                            String pathKey = Origin.host(keyUri) + "/hls/" + videoKey + "/enc.key";
                            add(out, sink, "key", "path", "okhttp",
                                    Http.okhttpOnly("https://" + pathKey, jar, null));
                        }
                    }
                }
            }
        }

        // --- webview transport (agar attach hai) ---
        if (BrowserNet.ready()) {
            for (Map.Entry<String, String> u : urls.entrySet()) {
                Downloader.Resp r = BrowserNet.getText(u.getValue());
                Row row = new Row();
                row.what = "master";
                row.variant = u.getKey();
                row.transport = "webview";
                row.status = r.status;
                row.bytes = r.body == null ? 0 : r.body.length;
                out.add(row);
                if (sink != null) sink.onRow(row);
            }
        }

        if (sink != null) sink.onDone(out);
        return out;
    }

    private static void addDoc(List<Row> out, Sink sink, String what, String variant,
                               DocFetch.Res d) {
        Row row = new Row();
        row.what = what;
        row.variant = variant;
        row.transport = "docnav";
        row.status = d.status;
        row.bytes = d.body == null ? 0 : d.body.length;
        row.preview = preview(d.text, d.status);
        row.server = serverOf(d.headers);
        if (d.note != null && !d.note.isEmpty()) {
            row.note = d.note.substring(0, Math.min(28, d.note.length()));
        }
        out.add(row);
        if (sink != null) sink.onRow(row);
    }

    private static void add(List<Row> out, Sink sink, String what, String variant,
                            String transport, Http.Res r) {
        Row row = new Row();
        row.what = what;
        row.variant = variant;
        row.transport = transport;
        row.status = r.status;
        row.bytes = r.body == null ? 0 : r.body.length;
        row.preview = preview(r.text, r.status);
        row.server = serverOf(r.headers);
        if (r.error != null && !r.error.isEmpty()) row.note = r.error.substring(0, Math.min(28, r.error.length()));
        out.add(row);
        if (sink != null) sink.onRow(row);
    }

    /** 502/403 kisne diya — Cloudflare edge (cf-ray) ya origin (server). */
    private static String serverOf(Map<String, String> h) {
        if (h == null || h.isEmpty()) return "";
        StringBuilder sb = new StringBuilder();
        for (String k : new String[]{"server", "cf-ray", "cf-cache-status", "x-served-by",
                "x-cache", "via", "x-powered-by"}) {
            for (Map.Entry<String, String> e : h.entrySet()) {
                if (e.getKey() != null && e.getKey().equalsIgnoreCase(k)) {
                    sb.append(k).append("=").append(e.getValue()).append(" ");
                }
            }
        }
        String out = sb.toString().trim();
        return out.length() > 90 ? out.substring(0, 90) : out;
    }

    /**
     * Body ka nichod. 502 ka jawab body ke ANDAR hota hai — par HTML ke head me
     * (Cloudflare ka boilerplate) nahi, uske aage. Isliye pehle <title>/<h1> aur
     * "cloudflare"/"worker"/"error code" wali line dhoondte hain, tab status line.
     */
    private static String preview(String text, int status) {
        if (text == null || text.isEmpty()) return "";
        try {
            if (text.contains("#EXTM3U")) {
                StringBuilder sb = new StringBuilder();
                for (String raw : text.replace("\r", "").split("\n")) {
                    String l = raw.trim();
                    if (l.startsWith("#EXT-X-STREAM-INF:") || l.startsWith("#EXT-X-KEY:")
                            || (!l.isEmpty() && !l.startsWith("#"))) {
                        // variant line: sirf resolution + path (poora URL base alag)
                        String one = l.replace("BANDWIDTH=", "").replace("RESOLUTION=", "");
                        sb.append(one.length() > 100 ? one.substring(0, 100) : one).append(" | ");
                        if (sb.length() > 260) break;
                    }
                }
                return sb.length() == 0 ? "(m3u8, koi variant line nahi)" : sb.toString();
            }
            String flat = text.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ');

            if (status >= 400) {
                // 1) Cloudflare/edge ke asli markers
                for (String key : new String[]{"<title>", "Error code", "error code", "error_code",
                        "Ray ID", "ray_id", "cloudflare", "Cloudflare", "worker", "origin",
                        "Web server is", "Bad gateway", "AccessDenied", "Access denied"}) {
                    int i = flat.indexOf(key);
                    if (i >= 0) {
                        int a = Math.max(0, i - 60);
                        int b = Math.min(flat.length(), i + 170);
                        return flat.substring(a, b).trim();
                    }
                }
                // 2) <h1>/<h2> ka text
                int h = flat.indexOf("<h1");
                if (h < 0) h = flat.indexOf("<h2");
                if (h >= 0) {
                    int b = Math.min(flat.length(), h + 200);
                    return flat.substring(h, b).trim();
                }
                // 3) kuch bhi na mile to aage ka hissa (head skip)
                int from = Math.min(flat.length(), Math.max(600, flat.length() / 3));
                return flat.substring(from, Math.min(flat.length(), from + 240)).trim();
            }

            String t = flat.trim();
            return t.length() > 140 ? t.substring(0, 140) : t;
        } catch (Exception e) {
            return "";
        }
    }

    private static String abs(String base, String rel) {
        try {
            return new java.net.URL(new java.net.URL(base), rel).toString();
        } catch (Exception e) {
            return rel;
        }
    }
}
