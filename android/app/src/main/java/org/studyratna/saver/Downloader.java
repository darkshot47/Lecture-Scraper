package org.studyratna.saver;

import android.content.Context;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import javax.crypto.Cipher;
import javax.crypto.spec.IvParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/**
 * Purana download kaam: master -> variant -> media playlist -> segments.
 *
 * Sab kuch native (HttpURLConnection). Isliye:
 *   - koi Node/Termux nahi chahiye
 *   - segments disk par cache hote hain (raw + decrypted) => resume free
 *   - key ek baar mil jaye to file turant banti hai (chahe token expire ho)
 *   - download ke baad yahi file MediaStore/ExoPlayer me jaati hai
 */
public final class Downloader {

    public static final String CACHE = "lecture-cache";

    /** Progress callbacks (background thread se aate hain; UI khud post kare). */
    public interface Listener {
        /** @param stage kya ho raha hai (insaan ke liye) */
        void onStage(String stage);

        /** @param done kitne segment ho gaye, total kitne, bytes kitne */
        void onProgress(int done, int total, long bytes);

        /** Segment fail (har segment par ek baar) */
        void onSegmentError(int index, String msg);
    }

    public static class Result {
        public File file;             // final .ts / .mp4 (cache me)
        public String title;
        public String duration;
        public int segments;
        public int failed;
        public long bytes;
        public long durationMs;
        public boolean keyMissing;
        public String keyHost;        // 401 aaya to kaunsa host
        public List<String> keyDiag = new ArrayList<>();

        public boolean ok() {
            return file != null && file.length() > 100_000;
        }
    }

    private static final String LABEL = "X-Label";

    private final Context ctx;
    private final Listener cb;

    /** Shared HTTP headers — WebView sniff (NetSniffer) se bhi enrich hote hain. */
    public final Map<String, String> extraHeaders = new LinkedHashMap<>();

    public Downloader(Context ctx, Listener cb) {
        this.ctx = ctx.getApplicationContext();
        this.cb = cb;
    }

    /* --------------------------------------------------------------- public -- */

    public Result run(Token token) throws Exception {
        Result res = new Result();
        res.title = token.title;
        res.duration = token.duration;

        String cookie = token.cookie == null ? "" : token.cookie;
        String ua = "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
                + "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36";

        stage("playlist laa rahe hain…");
        Resp master = fetchPlaylist(token.videoUrl, cookie, ua);
        if (master.status != 200) {
            throw new Exception("player ne HTTP " + master.status + " diya — network ya link badal ke try karein"
                    + (master.status == 502 ? " (Cloudflare 502: IP block — \"Page se download\" try karein)" : ""));
        }
        String masterText = master.text == null ? "" : master.text;

        String variantUrl = token.videoUrl;
        String best = "";
        List<String> lines = splitLines(masterText);
        for (int i = 0; i < lines.size(); i++) {
            String l = lines.get(i).trim();
            if (l.startsWith("#EXT-X-STREAM-INF:")) {
                String next = i + 1 < lines.size() ? lines.get(i + 1).trim() : "";
                if (!next.isEmpty() && !next.startsWith("#")) {
                    variantUrl = absolutize(token.videoUrl, next);
                    best = (m(l, "RESOLUTION=([0-9x]+)") + " ").trim();
                }
            }
        }
        Resp media = variantUrl.equals(token.videoUrl) ? master : fetchPlaylist(variantUrl, cookie, ua);
        if (media.status != 200) {
            throw new Exception("media playlist HTTP " + media.status
                    + (media.status == 502 ? " — Cloudflare IP block. \"Page se download\" (site ka apna button) use karein." : ""));
        }
        String mediaText = media.text == null ? "" : media.text;

        /* ------- segments ------- */
        List<String> segUrls = new ArrayList<>();
        String keyUri = null, keyIv = null;
        int seq = 0;
        double dur = 0;
        for (String raw : splitLines(mediaText)) {
            String l = raw.trim();
            if (l.isEmpty()) continue;
            if (l.startsWith("#EXT-X-MEDIA-SEQUENCE:")) {
                seq = (int) num(l.split(":")[1]);
            } else if (l.startsWith("#EXT-X-KEY:")) {
                String method = m(l, "METHOD=([^,]+)");
                String uri = m(l, "URI=\"([^\"]+)\"");
                keyIv = m(l, "IV=0x([0-9a-fA-F]+)");
                keyUri = uri == null ? null : absolutize(variantUrl, uri);
                if (method != null && method.equalsIgnoreCase("NONE")) keyUri = null;
            } else if (l.startsWith("#EXTINF:")) {
                dur = num(l.substring(8).split(",")[0]);
            } else if (!l.startsWith("#")) {
                segUrls.add(absolutize(variantUrl, l));
            }
        }
        int total = segUrls.size();
        if (total == 0) throw new Exception("playlist me segments nahi mile");
        res.segments = total;

        File dir = new File(ctx.getFilesDir(), CACHE);
        if (!dir.exists() && !dir.mkdirs()) throw new Exception("cache folder nahi ban paya");

        stage(String.format(Locale.US, "%d segments · %s · %s", total,
                mmss((int) Math.round(dur * total)), best.isEmpty() ? "AES-128" : best + " · AES-128"));

        /* ------- segments download + decrypt ------- */
        final File fdir = dir;
        final int fTotal = total;
        final AtomicInteger cursor = new AtomicInteger(0);
        final AtomicInteger doneCount = new AtomicInteger(0);
        final AtomicInteger failCount = new AtomicInteger(0);
        final long[] bytes = {0};
        final String fCookie = cookie, fUa = ua, fKeyUri = keyUri, fKeyIv = keyIv;
        final List<String> fSegUrls = segUrls;
        final List<String> errors = new ArrayList<>();

        int workers = 4;
        List<Thread> pool = new ArrayList<>();
        for (int w = 0; w < workers; w++) {
            Thread t = new Thread(() -> {
                while (true) {
                    if (res.keyMissing) return;
                    int i = cursor.getAndIncrement();
                    if (i >= fTotal) return;
                    File segFile = new File(fdir, String.format(Locale.US, "seg-%04d.bin", i));
                    if (segFile.exists() && segFile.length() > 500) {
                        doneCount.incrementAndGet();
                        synchronized (bytes) {
                            bytes[0] += segFile.length();
                        }
                        if (cb != null) cb.onProgress(doneCount.get(), fTotal, bytes[0]);
                        continue;
                    }
                    File rawFile = new File(fdir, String.format(Locale.US, "raw-%04d.bin", i));

                    try {
                        byte[] rawData;
                        if (rawFile.exists() && rawFile.length() > 500) {
                            rawData = readAll(rawFile);
                        } else {
                            Resp r = http(fSegUrls.get(i), fCookie, fUa);
                            if (r.status != 200) throw new Exception("segment HTTP " + r.status);
                            rawData = r.body;
                            write(rawFile, rawData);           // encrypted hi cache karo
                        }

                        byte[] out = rawData;
                        boolean plainTs = rawData.length > 188 && rawData[0] == 0x47;
                        if (fKeyUri != null && !plainTs) {
                            byte[] key = getKey(fKeyUri, fCookie, fUa, rawData, ivFor(i, fKeyIv));
                            if (key == null) {
                                synchronized (errors) {
                                    errors.add("AES key nahi mili (" + keyHost + ")");
                                }
                                res.keyMissing = true;
                                return;
                            }
                            out = aesDecrypt(key, rawData, ivFor(i, fKeyIv));
                        }
                        write(segFile, out);
                        synchronized (bytes) {
                            bytes[0] += out.length;
                        }
                    } catch (Exception e) {
                        failCount.incrementAndGet();
                        if (cb != null) cb.onSegmentError(i, e.getMessage() == null ? e.toString() : e.getMessage());
                    }
                    doneCount.incrementAndGet();
                    if (cb != null) cb.onProgress(doneCount.get(), fTotal, bytes[0]);
                }
            });
            t.setDaemon(true);
            pool.add(t);
            t.start();
        }
        for (Thread t : pool) t.join();

        res.failed = failCount.get();
        res.bytes = bytes[0];

        if (res.keyMissing) {
            res.keyHost = keyHost;
            res.keyDiag.addAll(keyDiag);
            return res;   // caller "key nahi mili" + cached segments dikhayega
        }
        if (doneCount.get() == 0) throw new Exception("ek bhi segment nahi aaya");

        /* ------- join ------- */
        stage("segments jod rahe hain…");
        File out = new File(dir, "lecture.ts");
        OutputStream os = new FileOutputStream(out);
        long written = 0;
        for (int i = 0; i < total; i++) {
            File f = new File(dir, String.format(Locale.US, "seg-%04d.bin", i));
            if (!f.exists()) continue;
            InputStream in = new FileInputStream(f);
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) {
                os.write(buf, 0, n);
                written += n;
            }
            in.close();
        }
        os.flush();
        os.close();
        res.file = out;
        res.bytes = written;
        return res;
    }


    /**
     * Playlist laao. Pehle normal, 502 par "browser mode" — matlab request
     * WebView ke andar hi jaati hai (wahi context jahan player chalta hai),
     * wahan se bytes wapas base64 me aate hain.
     *
     * KYUN: native 502 Cloudflare ka IP/TLS fingerprint block hai. WebView
     * ka request asli Chrome jaisa hota hai, isliye wahan se 200 aata hai.
     */
    /**
     * PAGE-SE-DOWNLOAD: playlist/segments/key sab page ke andar se (same origin)
     * maango — jo browser me chalta hai. Agar WebView page attached hai to yahi
     * default raasta hai, kyunki native request par Cloudflare 502 de raha hai.
     */
    public Result runViaPage(Token token) throws Exception {
        Result res = new Result();
        res.title = token.title;
        res.duration = token.duration;

        if (!BrowserNet.ready()) throw new Exception("page attach nahi hai — pehle page kholo");

        stage("page se playlist laa rahe hain…");
        Resp master = BrowserNet.getText(token.videoUrl);
        if (master.status != 200) {
            throw new Exception("page se bhi playlist HTTP " + master.status
                    + " — lecture page par play karke dobara try karein");
        }
        String masterText = master.text;

        String variantUrl = token.videoUrl;
        for (String l : masterText.replace("\r", "").split("\n")) {
            String t = l.trim();
            if (t.startsWith("#EXT-X-STREAM-INF:")) {
                // agla line hi variant hai — neeche parse me hi handle ho jayega
            }
        }
        int idx = 0;
        String[] ml = masterText.replace("\r", "").split("\n");
        for (int i = 0; i < ml.length; i++) {
            if (ml[i].trim().startsWith("#EXT-X-STREAM-INF:") && i + 1 < ml.length) {
                String next = ml[i + 1].trim();
                if (!next.isEmpty() && !next.startsWith("#")) {
                    variantUrl = absolutize(token.videoUrl, next);
                    idx++;
                }
            }
        }
        Resp media = variantUrl.equals(token.videoUrl) ? master : BrowserNet.getText(variantUrl);
        if (media.status != 200) throw new Exception("page se media playlist HTTP " + media.status);
        String mediaText = media.text;

        List<String> segUrls = new ArrayList<>();
        String keyUri = null, keyIv = null;
        for (String raw : splitLines(mediaText)) {
            String l = raw.trim();
            if (l.isEmpty()) continue;
            if (l.startsWith("#EXT-X-KEY:")) {
                String method = m(l, "METHOD=([^,]+)");
                String uri = m(l, "URI=\"([^\"]+)\"");
                keyIv = m(l, "IV=0x([0-9a-fA-F]+)");
                keyUri = uri == null ? null : absolutize(variantUrl, uri);
                if (method != null && method.equalsIgnoreCase("NONE")) keyUri = null;
            } else if (!l.startsWith("#")) {
                segUrls.add(absolutize(variantUrl, l));
            }
        }
        if (segUrls.isEmpty()) throw new Exception("page se playlist mili par segments nahi");
        res.segments = segUrls.size();

        File dir = new File(ctx.getFilesDir(), CACHE);
        if (!dir.exists() && !dir.mkdirs()) throw new Exception("cache folder nahi ban paya");

        byte[] key = null;
        if (keyUri != null) {
            stage("page se AES key maang rahe hain…");
            Resp k = BrowserNet.getText(keyUri);
            if (k.status == 200 && (k.body.length == 16 || k.body.length == 32)) {
                key = k.body;
                stage("✓ key mili (page se, " + key.length + " bytes)");
                try {
                    write(new File(dir, "hlskey.bin"), key);
                } catch (Exception ignored) {
                }
            } else {
                res.keyMissing = true;
                res.keyHost = "page";
                res.keyDiag.add("page fetch -> HTTP " + k.status + " (" + k.body.length + " bytes)");
                for (String s : BrowserNet.logLines()) res.keyDiag.add(s);
                return res;
            }
        }

        stage(String.format(Locale.US, "%d segments · page mode", res.segments));

        long totalBytes = 0;
        int failed = 0;
        int done = 0;
        for (int i = 0; i < segUrls.size(); i++) {
            File segFile = new File(dir, String.format(Locale.US, "seg-%04d.bin", i));
            File rawFile = new File(dir, String.format(Locale.US, "raw-%04d.bin", i));
            if (segFile.exists() && segFile.length() > 500) {
                done++;
                totalBytes += segFile.length();
                if (cb != null) cb.onProgress(done, segUrls.size(), totalBytes);
                continue;
            }
            try {
                byte[] rawData;
                if (rawFile.exists() && rawFile.length() > 500) {
                    rawData = readAll(rawFile);
                } else {
                    Resp r = BrowserNet.getText(segUrls.get(i));
                    if (r.status != 200) throw new Exception("seg HTTP " + r.status);
                    rawData = r.body;
                    write(rawFile, rawData);
                }
                boolean plainTs = rawData.length > 188 && rawData[0] == 0x47;
                byte[] out = rawData;
                if (key != null && !plainTs) out = aesDecrypt(key, rawData, ivFor(i, keyIv));
                write(segFile, out);
                totalBytes += out.length;
            } catch (Exception e) {
                failed++;
                if (cb != null) cb.onSegmentError(i, e.getMessage() == null ? e.toString() : e.getMessage());
            }
            done++;
            if (cb != null) cb.onProgress(done, segUrls.size(), totalBytes);
        }
        res.failed = failed;
        res.bytes = totalBytes;
        if (done - failed == 0) throw new Exception("page se ek bhi segment nahi aaya");

        stage("segments jod rahe hain…");
        File outFile = new File(dir, "lecture.ts");
        OutputStream os = new FileOutputStream(outFile);
        long written = 0;
        for (int i = 0; i < segUrls.size(); i++) {
            File f = new File(dir, String.format(Locale.US, "seg-%04d.bin", i));
            if (!f.exists()) continue;
            InputStream in = new FileInputStream(f);
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) {
                os.write(buf, 0, n);
                written += n;
            }
            in.close();
        }
        os.flush();
        os.close();
        res.file = outFile;
        res.bytes = written;
        return res;
    }

    private Resp fetchPlaylist(String url, String cookie, String ua) {
        Resp r = http(url, cookie, ua);
        stage("playlist -> HTTP " + r.status);
        if (r.status == 200 || r.status == 0) return r;
        Resp viaWeb = BrowserNet.getText(url);
        stage("browser mode -> HTTP " + viaWeb.status);
        if (viaWeb.status == 200) return viaWeb;
        return r;
    }

    private void stage(String s) {
        if (cb != null) cb.onStage(s);
    }

    /* ----------------------------------------------------------------- key -- */

    private String keyHost = "?";
    private final List<String> keyDiag = new ArrayList<>();
    private final Map<String, byte[]> keyMemo = new LinkedHashMap<>();
    private boolean keyLogged = false;

    /**
     * AES key laao. Raaste:
     *   0. pehle se cache (files/hlskey.bin), WebView sniff se mila header set
     *   1. header variants (app/player/api referer, client-id, bearer)
     *   2. URL shapes (path form, player proxy, CDN origin)
     *   3. sab fail -> null (caller "key nahi mili" bolta hai)
     */
    private synchronized byte[] getKey(String uri, String cookie, String ua, byte[] probe, byte[] iv) {
        byte[] memo = keyMemo.get(uri);
        if (memo != null) return memo;

        try {
            keyHost = new URL(uri).getHost();
        } catch (Exception ignored) {
        }

        // 0) disk cache
        File kf = new File(new File(ctx.getFilesDir(), CACHE), "hlskey.bin");
        if (kf.exists()) {
            byte[] d = readAll(kf);
            if (d.length == 16 || d.length == 32) {
                if (probe == null || keyWorks(d, probe, iv)) {
                    keyMemo.put(uri, d);
                    return d;
                }
            }
        }

        // 1) header variants x 2) URL shapes
        List<Map<String, String>> variants = headerVariants(cookie, ua);
        List<String> urls = keyUrlShapes(uri);

        for (String u : urls) {
            for (Map<String, String> h : variants) {
                Resp r = httpWithHeaders(u, h);
                if (r.status == 200 && (r.body.length == 16 || r.body.length == 32)
                        && (probe == null || keyWorks(r.body, probe, iv))) {
                    keyMemo.put(uri, r.body);
                    try {
                        write(kf, r.body);
                    } catch (Exception ignored) {
                        // cache likhna fail ho to bhi key kaam ki hai
                    }
                    if (!keyLogged) {
                        keyLogged = true;
                        stage("✓ AES key mil gayi (" + h.get("X-Label") + ")");
                    }
                    return r.body;
                }
                if (!keyDiag.contains(h.get("X-Label") + " -> " + r.status)) {
                    keyDiag.add(h.get("X-Label") + " -> HTTP " + r.status);
                }
            }
        }
        return null;
    }

    private List<Map<String, String>> headerVariants(String cookie, String ua) {
        List<Map<String, String>> out = new ArrayList<>();
        String app = "https://app.studyratna.org";

        String[][] refs = {
                {"app", app + "/", app},
                {"player", "https://player.studyratna.org/", "https://player.studyratna.org"},
                {"pw", "https://www.penpencil.co/", "https://www.penpencil.co"},
        };
        for (String[] r : refs) {
            Map<String, String> h = base(ua);
            h.put("Referer", r[1]);
            h.put("Origin", r[2]);
            if (!cookie.isEmpty()) h.put("Cookie", cookie);
            h.put("X-Label", r[0]);
            out.add(h);
        }
        Map<String, String> pw = base(ua);
        pw.put("Referer", app + "/");
        pw.put("client-id", "5eb393ee95fab7468a79d189");
        pw.put("client-type", "WEB");
        pw.put("client-version", "5.0.0");
        if (!cookie.isEmpty()) pw.put("Cookie", cookie);
        pw.put("X-Label", "pw-client");
        out.add(pw);

        // WebView ne jo headers dikhaye (asli jeet yahi hoti hai)
        if (!extraHeaders.isEmpty()) {
            Map<String, String> sniff = base(ua);
            sniff.putAll(extraHeaders);
            if (!cookie.isEmpty()) sniff.put("Cookie", cookie);
            sniff.put("X-Label", "sniffed");
            out.add(sniff);
        }
        return out;
    }

    private Map<String, String> base(String ua) {
        Map<String, String> h = new LinkedHashMap<>();
        h.put("User-Agent", ua);
        h.put("Accept", "*/*");
        h.put("Accept-Language", "en-IN,en;q=0.9");
        return h;
    }

    private List<String> keyUrlShapes(String keyUri) {
        List<String> out = new ArrayList<>();
        out.add(keyUri);
        String videoKey = "?";
        try {
            URL u = new URL(keyUri);
            String q = u.getQuery();
            if (q != null) {
                for (String kv : q.split("&")) {
                    if (kv.startsWith("videoKey=")) videoKey = kv.substring(9);
                }
            }
            if ("?".equals(videoKey)) {
                String[] parts = u.getPath().split("/");
                if (parts.length > 0) videoKey = parts[parts.length - 1];
            }
            String origin = u.getProtocol() + "://" + u.getHost();
            out.add(origin + "/hls/" + videoKey + "/enc.key");
            out.add(origin + "/stream/" + videoKey + "/enc.key");
            out.add(origin + "/stream/" + videoKey + "/hls/720/enc.key");
        } catch (Exception ignored) {
        }
        try {
            out.add("https://player.studyratna.org/?url=" + java.net.URLEncoder.encode(keyUri, "UTF-8"));
        } catch (Exception ignored) {
        }
        return out;
    }

    /** Decrypt karke MPEG-TS sync byte (0x47) dekho — 200 par galat key bhi aa sakti hai. */
    private static boolean keyWorks(byte[] key, byte[] raw, byte[] iv) {
        try {
            int n = Math.min(raw.length, 188 * 5);
            int len = (int) (Math.ceil(n / 16.0) * 16);
            byte[] head = new byte[len];
            System.arraycopy(raw, 0, head, 0, Math.min(raw.length, len));
            byte[] d = aesDecrypt(key, head, iv);
            for (int off = 0; off < 188; off++) {
                if (off + 376 < d.length && d[off] == 0x47 && d[off + 188] == 0x47 && d[off + 376] == 0x47) {
                    return true;
                }
            }
        } catch (Exception ignored) {
        }
        return false;
    }

    static byte[] aesDecrypt(byte[] key, byte[] data, byte[] iv) throws Exception {
        Cipher c = Cipher.getInstance("AES/CBC/NoPadding");
        int len = data.length - (data.length % 16);
        c.init(Cipher.DECRYPT_MODE, new SecretKeySpec(key, "AES"), new IvParameterSpec(iv));
        byte[] out = c.doFinal(data, 0, len);
        // PKCS#5 padding hatao (agar valid ho)
        if (out.length > 0) {
            int pad = out[out.length - 1] & 0xff;
            if (pad >= 1 && pad <= 16 && pad <= out.length) {
                boolean okPad = true;
                for (int i = out.length - pad; i < out.length; i++) {
                    if ((out[i] & 0xff) != pad) {
                        okPad = false;
                        break;
                    }
                }
                if (okPad) {
                    byte[] t = new byte[out.length - pad];
                    System.arraycopy(out, 0, t, 0, t.length);
                    return t;
                }
            }
        }
        return out;
    }

    private static byte[] ivFor(int seq, String ivHex) {
        byte[] iv = new byte[16];
        if (ivHex != null && !ivHex.isEmpty()) {
            String h = ivHex.length() >= 32 ? ivHex.substring(ivHex.length() - 32) : ivHex;
            while (h.length() < 32) h = "0" + h;
            for (int i = 0; i < 16; i++) iv[i] = (byte) Integer.parseInt(h.substring(i * 2, i * 2 + 2), 16);
        } else {
            long n = seq;
            for (int i = 15; i >= 0 && n > 0; i--) {
                iv[i] = (byte) (n & 0xff);
                n >>= 8;
            }
        }
        return iv;
    }

    /* ------------------------------------------------------------------ http -- */

    static class Resp {
        int status;
        byte[] body;
        String text;
    }

    private Resp http(String url, String cookie, String ua) {
        Map<String, String> h = base(ua);
        h.put("Referer", "https://app.studyratna.org/");
        h.put("Origin", "https://app.studyratna.org");
        if (!cookie.isEmpty()) h.put("Cookie", cookie);
        return httpWithHeaders(url, h);
    }

    private Resp httpWithHeaders(String url, Map<String, String> headers) {
        Resp out = new Resp();
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setConnectTimeout(20000);
            c.setReadTimeout(30000);
            c.setInstanceFollowRedirects(true);
            c.setRequestProperty("Accept-Encoding", "identity");
            for (Map.Entry<String, String> e : headers.entrySet()) {
                if (LABEL.equals(e.getKey())) continue;   // internal label, header nahi
                c.setRequestProperty(e.getKey(), e.getValue());
            }
            out.status = c.getResponseCode();
            InputStream in = out.status >= 400 ? c.getErrorStream() : c.getInputStream();
            out.body = in == null ? new byte[0] : readAll(in);
            out.text = new String(out.body, StandardCharsets.UTF_8);
        } catch (Exception e) {
            out.status = 0;
            out.body = new byte[0];
            out.text = "";
        } finally {
            if (c != null) c.disconnect();
        }
        return out;
    }

    /* ------------------------------------------------------------------ util -- */

    private static List<String> splitLines(String s) {
        List<String> out = new ArrayList<>();
        for (String l : s.replace("\r", "").split("\n")) out.add(l);
        return out;
    }

    private static String m(String s, String rx) {
        java.util.regex.Matcher mt = java.util.regex.Pattern.compile(rx).matcher(s);
        return mt.find() ? mt.group(1) : null;
    }

    private static double num(String s) {
        try {
            return Double.parseDouble(s.trim());
        } catch (Exception e) {
            return 0;
        }
    }

    private static String absolutize(String base, String rel) {
        try {
            return new URL(new URL(base), rel).toString();
        } catch (Exception e) {
            return rel;
        }
    }

    static String mmss(int sec) {
        return String.format(Locale.US, "%dm%02ds", sec / 60, sec % 60);
    }

    static byte[] readAll(File f) {
        try {
            InputStream in = new FileInputStream(f);
            byte[] b = readAll(in);
            in.close();
            return b;
        } catch (Exception e) {
            return new byte[0];
        }
    }

    static byte[] readAll(InputStream in) throws Exception {
        BufferedInputStream bin = new BufferedInputStream(in, 1 << 16);
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[1 << 16];
        int n;
        while ((n = bin.read(buf)) > 0) bos.write(buf, 0, n);
        return bos.toByteArray();
    }

    static void write(File f, byte[] data) throws Exception {
        FileOutputStream fos = new FileOutputStream(f);
        fos.write(data);
        fos.flush();
        fos.close();
    }
}
