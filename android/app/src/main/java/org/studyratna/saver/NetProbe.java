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

        public String line() {
            if (preview != null && !preview.isEmpty()) {
                return String.format(java.util.Locale.US, "%-6s %-9s %-8s -> %3d  %6d B %s%n        | %s",
                        what, variant, transport, status, bytes, note, preview);
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
                String mediaCdn = videoKey == null ? null
                        : Origin.toCdn(mediaPlayer, videoKey);
                add(out, sink, "media", "player", "okhttp", Http.okhttpOnly(mediaPlayer, jar, null));
                if (SameOrigin.ready()) {
                    add(out, sink, "media", "player", "sameorg", Http.sameOriginOnly(mediaPlayer, jar));
                }
                if (DocFetch.ready()) {
                    addDoc(out, sink, "media", "player", DocFetch.get(mediaPlayer, jar, 25000));
                }
                if (mediaCdn != null && !mediaCdn.equals(mediaPlayer)) {
                    add(out, sink, "media", "cdn", "okhttp", Http.okhttpOnly(mediaCdn, jar, null));
                }

                // ek media playlist se seg0 + key
                Http.Res mp = Http.okhttpOnly(mediaCdn != null ? mediaCdn : mediaPlayer, jar, null);
                if (mp.status != 200) mp = Http.okhttpOnly(mediaPlayer, jar, null);
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
        if (r.error != null && !r.error.isEmpty()) row.note = r.error.substring(0, Math.min(28, r.error.length()));
        out.add(row);
        if (sink != null) sink.onRow(row);
    }

    /**
     * Body ka nichod. 502 ka jawab yahin chhupa hota hai (6458 B me), aur master
     * (494 B) me asli variant URL. Inhe padhe bina koi fix guess hi hoga.
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
                        sb.append(l.length() > 110 ? l.substring(0, 110) : l).append(" | ");
                        if (sb.length() > 300) break;
                    }
                }
                return sb.length() == 0 ? "(m3u8, koi variant line nahi)" : sb.toString();
            }
            if (status >= 400) {
                String t = text.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ').trim();
                return t.length() > 260 ? t.substring(0, 260) : t;
            }
            String t = text.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ').trim();
            return t.length() > 120 ? t.substring(0, 120) : t;
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
