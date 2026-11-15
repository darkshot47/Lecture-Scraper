package org.studyratna.saver;

import android.util.Base64;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import javax.crypto.Cipher;
import javax.crypto.spec.IvParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/**
 * StudyRatna playerToken ko kholta hai.
 *
 * Token shape: "ivHex:ctHex", AES-256-CBC, key = SHA-256(TOKEN_SEED).
 * Poore URL (…?playerToken=…), bare token, %3A-encoded blob aur quoted string —
 * sab chalta hai. (Wahi resolveToken() jo local/download.mjs me hai.)
 */
public final class Token {

    static final String SEED = "5d90298d9598093cf8cb4b55434de5bc";

    public final JSONObject data;
    public final String videoUrl;
    public final String cookie;
    public final String title;
    public final String duration;
    public final String initData;
    /** Is Token object ko banane wala asli string (dobara parse na ho). */
    public String raw;

    private Token(JSONObject data, String videoUrl, String cookie, String title,
                  String duration, String initData) {
        this.data = data;
        this.videoUrl = videoUrl;
        this.cookie = cookie;
        this.title = title;
        this.duration = duration;
        this.initData = initData;
    }

    public String label() {
        String t = title == null || title.isEmpty() ? "Lecture" : title;
        return duration == null || duration.isEmpty() ? t : t + " · " + duration;
    }

    /* ------------------------------------------------------------- resolve -- */

    private static final Pattern P_TOKEN = Pattern.compile("playerToken=([^&\\s\"'#]+)", Pattern.CASE_INSENSITIVE);

    /** Poora URL / bare token / encoded blob -> {iv, ct}. */
    static String[] resolve(String raw) throws Exception {
        String s = raw == null ? "" : raw.trim();
        if (s.length() > 1 && (s.startsWith("\"") && s.endsWith("\""))) {
            s = s.substring(1, s.length() - 1).trim();
        }

        Matcher m = P_TOKEN.matcher(s);
        if (m.find()) {
            s = m.group(1);
            s = decode(s);
        } else if (!s.contains(":") && s.toUpperCase().contains("%3A")) {
            s = decode(s);
        }
        s = s.trim();

        if (s.startsWith("http://") || s.startsWith("https://")) {
            throw new Exception("link me playerToken= nahi mila — StudyRatna ka poora lecture link chahiye");
        }

        String[] parts = s.split(":");
        if (parts.length != 2) {
            throw new Exception("token galat (" + parts.length + " hisse mile, 2 chahiye) — poora lecture link paste karein");
        }
        String iv = parts[0].trim();
        String ct = parts[1].trim();
        if (!iv.matches("[0-9a-fA-F]{32}")) {
            throw new Exception("IV galat (32 hex chahiye, mile " + iv.length() + ") — link adhoora paste hua");
        }
        if (!ct.matches("[0-9a-fA-F]+") || ct.length() < 32 || ct.length() % 32 != 0) {
            throw new Exception("encrypted part galat (" + ct.length() + " hex) — link poora copy karein");
        }
        return new String[]{iv, ct};
    }

    private static String decode(String s) {
        try {
            return java.net.URLDecoder.decode(s, "UTF-8");
        } catch (Exception e) {
            return s;
        }
    }

    private static byte[] hex(String s) {
        int n = s.length() / 2;
        byte[] out = new byte[n];
        for (int i = 0; i < n; i++) {
            out[i] = (byte) Integer.parseInt(s.substring(i * 2, i * 2 + 2), 16);
        }
        return out;
    }

    /* ------------------------------------------------------------- decrypt -- */

    public static Token parse(String raw) throws Exception {
        String[] p = resolve(raw);
        byte[] iv = hex(p[0]);
        byte[] ct = hex(p[1]);

        MessageDigest sha = MessageDigest.getInstance("SHA-256");
        byte[] key = sha.digest(SEED.getBytes(StandardCharsets.UTF_8));

        Cipher c = Cipher.getInstance("AES/CBC/PKCS5Padding");
        c.init(Cipher.DECRYPT_MODE, new SecretKeySpec(key, "AES"), new IvParameterSpec(iv));
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.write(c.doFinal(ct));

        JSONObject o = new JSONObject(new String(out.toByteArray(), StandardCharsets.UTF_8));

        String videoUrl = o.optString("videoUrl", "");
        if (videoUrl.isEmpty()) videoUrl = o.optString("url", "");
        String cookie = o.optString("cookie", "");
        String initData = o.optString("initData", "");

        String title = "";
        String duration = "";
        JSONObject lec = o.optJSONObject("lecture");
        if (lec != null) {
            title = lec.optString("topic", "");
            JSONObject vd = lec.optJSONObject("videoDetails");
            if (vd != null) {
                if (title.isEmpty()) title = vd.optString("name", "");
                duration = vd.optString("duration", "");
            }
        }
        return new Token(o, videoUrl, cookie, title, duration, initData);
    }

    /** Base64 helper (bridge / caching ke liye). */
    @SuppressWarnings("unused")
    static String b64(byte[] b) {
        return Base64.encodeToString(b, Base64.NO_WRAP);
    }

    @SuppressWarnings("unused")
    static byte[] unb64(String s) {
        return Base64.decode(s, Base64.NO_WRAP);
    }
}
