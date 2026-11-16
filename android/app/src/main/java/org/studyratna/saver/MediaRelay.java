package org.studyratna.saver;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * MediaRelay — app ke andar chhota HTTP server (127.0.0.1) jo ek **rewritten
 * HLS playlist** serve karta hai.
 *
 * KYUN (NETWORK PROBE se mila data):
 *   master player (okhttp) -> 200
 *   media  player (okhttp) -> 502
 *   media  cdn    (okhttp) -> 403
 *
 * Matlab: master milta hai, variant playlist nahi. Par WebView me page chalta
 * hai, to WebView se wahi variant maangwa kar, us playlist ke andar ke segment
 * URLs ko **apne localhost** par point karwa dete hain. ExoPlayer phir:
 *   http://127.0.0.1:PORT/index.m3u8
 * chalata hai — koi Cloudflare nahi, koi CORS nahi. Segments yahan se aate hain
 * aur wahan se jo raasta 200 de (CDN/player/WebView) wo khud chunte hain.
 *
 * Ye "jugad" isliye kaam karta hai kyunki Cloudflare ke liye request wahi
 * player host ki hai (WebView se), aur player ko diya jaane wala playlist
 * hamare control me hai.
 */
public final class MediaRelay {

    public interface Log {
        void run(String line);
    }

    private final String videoUrl;
    private final String cookie;
    private final String videoKey;
    private final Log log;

    private volatile ServerSocket server;
    private Thread thread;
    private volatile boolean running = false;

    private volatile int port = 0;
    private volatile String variantUrl;      // variant playlist ka asli URL (WebView se mila)
    private volatile String mediaBase;       // variant URL ka base (relative segments ke liye)
    private volatile List<String> segmentUrls = new ArrayList<>();
    private volatile String keyUrl;
    private volatile String keyIv;

    public MediaRelay(String videoUrl, String cookie, Log log) {
        this.videoUrl = videoUrl;
        this.cookie = cookie == null ? "" : cookie;
        this.videoKey = Origin.videoKey(videoUrl);
        this.log = log;
    }

    public String localUrl() {
        return port == 0 ? null : "http://127.0.0.1:" + port + "/index.m3u8";
    }

    public int port() {
        return port;
    }

    /* ------------------------------------------------------------------ start -- */

    /** Playlist resolve karo aur server chalao. Blocking (background thread se). */
    public boolean start() {
        try {
            if (!resolvePlaylist()) return false;
            server = new ServerSocket(0, 8, InetAddress.getByName("127.0.0.1"));
            port = server.getLocalPort();
            running = true;
            thread = new Thread(this::acceptLoop, "media-relay");
            thread.setDaemon(true);
            thread.start();
            say("relay chalu: " + localUrl() + "  (" + segmentUrls.size() + " segments)");
            return true;
        } catch (Exception e) {
            say("relay start fail: " + e.getMessage());
            return false;
        }
    }

    public void stop() {
        running = false;
        try {
            if (server != null) server.close();
        } catch (Exception ignored) {
        }
        server = null;
        port = 0;
    }

    private void say(String s) {
        if (log != null) log.run(s);
    }

    /* -------------------------------------------------------------- playlist -- */

    private boolean resolvePlaylist() {
        for (int attempt = 0; attempt < 3; attempt++) {
            if (attempt > 0) {
                say("retry " + attempt + "…");
                try { Thread.sleep(1200L * attempt); } catch (InterruptedException ignored) {}
            }
            if (resolveOnce()) return true;
        }
        return false;
    }

    private boolean resolveOnce() {
        // 1) master — jo bhi raasta 200 de
        Downloader.Resp master = fetchBest(videoUrl, true);
        if (master.status != 200 || !master.text.contains("#EXTM3U")) {
            say("master fail: HTTP " + master.status);
            return false;
        }
        say("master mila (" + master.body.length + " B)");

        // 2) saare variants — 720 par 502 ho to 480/360 par dekhna (server-side
        //    transcode ho sakta hai; ek quality zinda ho to kaam ho jayega)
        List<String> variants = new ArrayList<>();
        String[] ls = master.text.replace("\r", "").split("\n");
        for (int i = 0; i < ls.length; i++) {
            if (ls[i].trim().startsWith("#EXT-X-STREAM-INF:") && i + 1 < ls.length) {
                String n = ls[i + 1].trim();
                if (!n.isEmpty() && !n.startsWith("#")) variants.add(abs(videoUrl, n));
            }
        }
        if (variants.isEmpty()) variants.add(videoUrl);

        Downloader.Resp media = null;
        String mediaUrl = null;
        for (String v : variants) {
            Downloader.Resp r = fetchBest(v, true);
            if (r.status == 200 && r.text.contains("#EXTINF")) {
                media = r;
                mediaUrl = v;
                break;
            }
            say("  variant fail: " + v.substring(Math.max(0, v.lastIndexOf("/hls/"))) + " -> " + r.status);
        }
        if (media == null) {
            say("media fail: sab variants 502 (WebView/docnav se bhi nahi aaya)");
            return false;
        }
        say("media mila (" + media.body.length + " B) — " + mediaUrl);
        variantUrl = mediaUrl;
        mediaBase = mediaUrl;

        // 3) parse segments + key
        for (String raw : media.text.replace("\r", "").split("\n")) {
            String l = raw.trim();
            if (l.isEmpty()) continue;
            if (l.startsWith("#EXT-X-KEY:")) {
                java.util.regex.Matcher m =
                        java.util.regex.Pattern.compile("URI=\"([^\"]+)\"").matcher(l);
                if (m.find()) keyUrl = abs(mediaBase, m.group(1));
                java.util.regex.Matcher iv =
                        java.util.regex.Pattern.compile("IV=0x([0-9a-fA-F]+)").matcher(l);
                if (iv.find()) keyIv = iv.group(1);
            } else if (!l.startsWith("#")) {
                segmentUrls.add(abs(mediaBase, l));
            }
        }
        if (segmentUrls.isEmpty()) {
            say("playlist me segments nahi");
            return false;
        }
        say("segments: " + segmentUrls.size() + (keyUrl == null ? " · plain" : " · AES-128"));
        return true;
    }

    /** Kaun sa raasta 200 deta hai — kram se try. */
    private Downloader.Resp fetchBest(String url, boolean playlist) {
        List<String> cands = new ArrayList<>();
        cands.add(url);
        if (videoKey != null) {
            String cdn = Origin.toCdnOn(url, videoKey, Origin.cdnBase(cookie));
            if (cdn != null && !cdn.equals(url)) cands.add(cdn);
        }
        for (String c : cands) {
            if (DocFetch.ready()) {
                DocFetch.Res d = DocFetch.get(c, cookie, 25000);
                if (d.ok()) {
                    say("  ✓ docnav: " + Origin.host(c));
                    Downloader.Resp out = new Downloader.Resp();
                    out.status = d.status;
                    out.body = d.body;
                    out.text = d.text;
                    return out;
                }
                say("  ✗ docnav " + Origin.host(c) + " -> " + d.status
                        + (d.note.isEmpty() ? "" : " " + d.note));
            }
            if (SameOrigin.ready()) {
                Downloader.Resp s = SameOrigin.get(c, cookie, 25000);
                if (s.status == 200) {
                    say("  ✓ same-origin: " + Origin.host(c));
                    return s;
                }
                say("  ✗ same-origin " + Origin.host(c) + " -> " + s.status);
            }
            Http.Res r = Http.okhttpOnly(c, cookie, null);
            if (r.status == 200) {
                say("  ✓ okhttp: " + Origin.host(c));
                Downloader.Resp out = new Downloader.Resp();
                out.status = 200;
                out.body = r.body;
                out.text = r.text;
                return out;
            }
            say("  ✗ okhttp " + Origin.host(c) + " -> " + r.status);
        }
        Downloader.Resp last = new Downloader.Resp();
        last.status = 502;
        return last;
    }

    /* ---------------------------------------------------------------- serving -- */

    private void acceptLoop() {
        while (running) {
            try {
                final Socket s = server.accept();
                Thread t = new Thread(() -> handle(s), "relay-req");
                t.setDaemon(true);
                t.start();
            } catch (Exception e) {
                if (running) say("accept: " + e.getMessage());
            }
        }
    }

    private void handle(Socket sock) {
        try {
            InputStream in = sock.getInputStream();
            ByteArrayOutputStream head = new ByteArrayOutputStream();
            int prev1 = -1, prev2 = -1, b;
            while ((b = in.read()) >= 0) {
                head.write(b);
                if (prev3(prev1, prev2, b) && head.size() > 4) break;
                prev1 = prev2;
                prev2 = b;
                if (head.size() > 8192) break;
            }
            String request = head.toString("UTF-8");
            String path = "/";
            if (request.startsWith("GET ")) {
                int i = request.indexOf(' ');
                int j = request.indexOf(' ', i + 1);
                if (j > i) path = request.substring(i + 1, j);
            }
            String q = "";
            int qi = path.indexOf('?');
            if (qi >= 0) {
                q = path.substring(qi + 1);
                path = path.substring(0, qi);
            }

            OutputStream out = sock.getOutputStream();
            if (path.equals("/index.m3u8") || path.equals("/")) {
                byte[] body = buildPlaylist().getBytes(StandardCharsets.UTF_8);
                write(out, "application/vnd.apple.mpegurl", body, 200);
            } else if (path.startsWith("/key")) {
                byte[] key = fetchKey();
                if (key == null) write(out, "text/plain", "no key".getBytes(), 502);
                else write(out, "application/octet-stream", key, 200);
            } else if (path.startsWith("/seg/")) {
                int idx = parseInt(path.substring(5));
                byte[] data = fetchSegment(idx);
                if (data == null) write(out, "text/plain", "seg fail".getBytes(), 502);
                else write(out, "video/mp2t", data, 200);
            } else {
                write(out, "text/plain", "not found".getBytes(), 404);
            }
            out.flush();
            sock.close();
        } catch (Exception ignored) {
        }
    }

    private static boolean prev3(int a, int b, int c) {
        return a == '\r' && b == '\n' && c == '\n';
    }

    private void write(OutputStream out, String mime, byte[] body, int code) throws Exception {
        String h = "HTTP/1.1 " + code + (code == 200 ? " OK" : " ERR") + "\r\n"
                + "Content-Type: " + mime + "\r\n"
                + "Content-Length: " + body.length + "\r\n"
                + "Cache-Control: no-store\r\n"
                + "Connection: close\r\n\r\n";
        out.write(h.getBytes(StandardCharsets.UTF_8));
        out.write(body);
    }

    /** Playlist rewritten: key + segments localhost par. */
    private String buildPlaylist() {
        StringBuilder sb = new StringBuilder();
        sb.append("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-PLAYLIST-TYPE:VOD\n");
        sb.append("#EXT-X-TARGETDURATION:10\n#EXT-X-MEDIA-SEQUENCE:0\n");
        if (keyUrl != null) {
            sb.append("#EXT-X-KEY:METHOD=AES-128,URI=\"http://127.0.0.1:").append(port)
                    .append("/key.bin\"");
            if (keyIv != null) sb.append(",IV=0x").append(keyIv);
            sb.append("\n");
        }
        for (int i = 0; i < segmentUrls.size(); i++) {
            sb.append(String.format(Locale.US, "#EXTINF:%.3f,\n", 6.0));
            sb.append("http://127.0.0.1:").append(port).append("/seg/").append(i).append("\n");
        }
        sb.append("#EXT-X-ENDLIST\n");
        return sb.toString();
    }

    private byte[] fetchSegment(int idx) {
        if (idx < 0 || idx >= segmentUrls.size()) return null;
        String url = segmentUrls.get(idx);
        for (int attempt = 0; attempt < 2; attempt++) {
            String u = url;
            if (videoKey != null) {
                String cdn = Origin.toCdnOn(url, videoKey, Origin.cdnBase(cookie));
                if (cdn != null) u = attempt == 0 ? url : cdn;
            }
            if (DocFetch.ready()) {
                DocFetch.Res d = DocFetch.get(u, cookie, 20000);
                if (d.ok()) return d.body;
            }
            if (SameOrigin.ready()) {
                Downloader.Resp s = SameOrigin.get(u, cookie, 20000);
                if (s.status == 200 && s.body.length > 0) return s.body;
            }
            Http.Res r = Http.okhttpOnly(u, cookie, null);
            if (r.status == 200 && r.body.length > 0) return r.body;
        }
        say("seg " + idx + " fail (dono raaste)");
        return null;
    }

    private byte[] fetchKey() {
        if (keyUrl == null) return null;
        List<String> cands = new ArrayList<>();
        cands.add(keyUrl);
        if (videoKey != null) {
            cands.add(Origin.cdnBase(cookie) + "/" + videoKey + "/enc.key");
        }
        for (String c : cands) {
            if (SameOrigin.ready()) {
                Downloader.Resp s = SameOrigin.get(c, cookie, 15000);
                if (s.status == 200 && keyOk(s.body)) return s.body;
            }
            Http.Res r = Http.okhttpOnly(c, cookie, null);
            if (r.status == 200 && keyOk(r.body)) return r.body;
        }
        say("key fail: " + keyUrl);
        return null;
    }

    /** AES-128/192/256 key hi valid (16/24/32 B). 200 me galat body bhi aa sakti hai. */
    private static boolean keyOk(byte[] k) {
        return k != null && (k.length == 16 || k.length == 24 || k.length == 32);
    }

    private static int parseInt(String s) {
        try {
            return Integer.parseInt(s.replaceAll("[^0-9].*$", ""));
        } catch (Exception e) {
            return -1;
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
