package org.studyratna.saver;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ContentValues;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.File;
import java.io.OutputStream;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Lecture Saver 2.0
 *
 *   [ link paste ] -> [ Dekho (player) ]  ya  [ Download (Gallery) ]
 *
 * Termux ki zaroorat nahi:
 *   - token app hi kholta hai (Token.java)
 *   - HLS + AES native (Downloader.java)
 *   - key ke liye player ke asli headers/cookies WebView sniff se
 *     (NetSniffer.java) — yahi wo cheez hai jo terminal se mumkin nahi thi
 *   - video app me hi chalti hai (PlayerActivity, ExoPlayer)
 *   - download seedha Gallery me (Movies/LectureSaver)
 */
public class MainActivity extends Activity {

    private static final String DEFAULT_URL = "https://app.studyratna.org/";

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService bg = Executors.newSingleThreadExecutor();

    private EditText urlBox;
    private TextView status;
    private TextView details;
    private Button watchBtn;
    private Button saveBtn;
    private Button sniffBtn;

    private Token token;
    private Downloader.Result lastResult;
    private boolean sniffing = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        float d = getResources().getDisplayMetrics().density;
        int pad = (int) (14 * d);

        ScrollView scroll = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, pad, pad, pad);
        scroll.addView(root);
        setContentView(scroll);
        askLegacyStorage();

        TextView title = new TextView(this);
        title.setText("Lecture Saver");
        title.setTextSize(21f);
        title.setTextColor(0xFF111827);
        root.addView(title);

        TextView hint = new TextView(this);
        hint.setText("StudyRatna ka lecture link paste karein. \"Dekho\" se video app me hi chalega, "
                + "\"Download\" se Gallery me save hogi.");
        hint.setTextSize(12f);
        hint.setTextColor(0xFF6B7280);
        hint.setPadding(0, (int) (6 * d), 0, (int) (10 * d));
        root.addView(hint);

        /* ---- link box ---- */
        urlBox = new EditText(this);
        urlBox.setHint("https://app.studyratna.org/?playerToken=…");
        urlBox.setTextSize(11f);
        urlBox.setSingleLine(false);
        urlBox.setMinLines(2);
        urlBox.setMaxLines(4);
        urlBox.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        urlBox.setVerticalScrollBarEnabled(true);
        root.addView(urlBox, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setPadding(0, (int) (6 * d), 0, (int) (10 * d));
        row.addView(small("Paste", v -> pasteLink()));
        row.addView(small("Clear", v -> urlBox.setText("")));
        row.addView(small("Open page", v -> openPage()));
        root.addView(row);

        /* ---- main buttons ---- */
        watchBtn = big("▶  Dekho (player)", 0xFF2563EB);
        watchBtn.setOnClickListener(v -> watch());
        root.addView(watchBtn);

        saveBtn = big("⬇  Download (Gallery me save)", 0xFF059669);
        saveBtn.setOnClickListener(v -> download());
        root.addView(saveBtn);

        sniffBtn = big("⚡ Key sniff (page se headers seekho)", 0xFF6B7280);
        sniffBtn.setTextSize(13f);
        sniffBtn.setOnClickListener(v -> sniff());
        root.addView(sniffBtn);

        status = new TextView(this);
        status.setTextSize(13f);
        status.setTextColor(0xFF111827);
        status.setPadding(0, (int) (12 * d), 0, 0);
        status.setText("status: link paste karein");
        root.addView(status);

        details = new TextView(this);
        details.setTextSize(11f);
        details.setTextColor(0xFF6B7280);
        details.setPadding(0, (int) (8 * d), 0, (int) (16 * d));
        details.setTextIsSelectable(true);
        root.addView(details);

        String incoming = getIntent() != null ? getIntent().getStringExtra("url") : null;
        urlBox.setText(incoming != null ? incoming : "");
    }

    /* --------------------------------------------------------------- UI bits -- */

    private Button small(String text, android.view.View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextSize(12f);
        b.setOnClickListener(l);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                0, (int) (44 * getResources().getDisplayMetrics().density), 1f);
        lp.setMargins(0, 0, (int) (6 * getResources().getDisplayMetrics().density), 0);
        b.setLayoutParams(lp);
        return b;
    }

    private Button big(String text, int color) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextSize(16f);
        b.setTextColor(0xFFFFFFFF);
        b.setBackgroundColor(color);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                (int) (52 * getResources().getDisplayMetrics().density));
        lp.setMargins(0, (int) (4 * getResources().getDisplayMetrics().density), 0,
                (int) (4 * getResources().getDisplayMetrics().density));
        b.setLayoutParams(lp);
        return b;
    }

    private void say(String s) {
        ui.post(() -> status.setText(s));
    }

    private void toast(String s) {
        ui.post(() -> Toast.makeText(this, s, Toast.LENGTH_SHORT).show());
    }

    private void pasteLink() {
        try {
            android.content.ClipboardManager cm =
                    (android.content.ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
            if (cm != null && cm.hasPrimaryClip() && cm.getPrimaryClip().getItemCount() > 0) {
                CharSequence t = cm.getPrimaryClip().getItemAt(0).coerceToText(this);
                if (t != null && t.length() > 0) {
                    urlBox.setText(t.toString().trim());
                    toast("link paste ho gaya");
                    return;
                }
            }
            toast("clipboard khali hai");
        } catch (Exception e) {
            toast("paste fail: " + e.getMessage());
        }
    }

    /* ----------------------------------------------------------------- token -- */

    /** Link ko token me badlo (background thread). Fail par dialog. */
    private Token tokenNow() throws Exception {
        String raw = urlBox.getText().toString().trim();
        if (raw.isEmpty()) throw new Exception("pehle link paste karein");
        if (token != null && raw.equals(token.raw)) return token;
        Token t = Token.parse(raw);
        t.raw = raw;
        token = t;
        return t;
    }

    /* ---------------------------------------------------------------- watch -- */

    private void watch() {
        say("token khol rahe hain…");
        bg.execute(() -> {
            try {
                Token t = tokenNow();
                if (t.videoUrl == null || t.videoUrl.isEmpty()) {
                    throw new Exception("token me videoUrl nahi hai");
                }
                NetSniffer.absorbCookiesFrom(t.videoUrl);
                String cookies = t.cookie == null ? "" : t.cookie;
                String sniffed = NetSniffer.cookieHeader(t.videoUrl);
                final String all = sniffed.isEmpty() ? cookies
                        : (cookies.isEmpty() ? sniffed : cookies + "; " + sniffed);

                final String url = t.videoUrl;
                ui.post(() -> {
                    details.setText("video: " + url + "\ntitle: " + t.label());
                    Intent i = new Intent(this, PlayerActivity.class);
                    i.putExtra(PlayerActivity.EXTRA_URL, url);
                    i.putExtra(PlayerActivity.EXTRA_COOKIE, all);
                    startActivity(i);
                });
            } catch (Exception e) {
                say("✗ " + e.getMessage());
            }
        });
    }

    /* ------------------------------------------------------------- download -- */

    private void download() {
        say("token khol rahe hain…");
        saveBtn.setEnabled(false);
        bg.execute(() -> {
            try {
                Token t = tokenNow();
                ui.post(() -> details.setText("title: " + t.label() + "\nsegments cache: "
                        + new File(getFilesDir(), Downloader.CACHE).getAbsolutePath()));

                Downloader dl = new Downloader(this, new Downloader.Listener() {
                    @Override
                    public void onStage(String stage) {
                        say(stage);
                    }

                    @Override
                    public void onProgress(int done, int total, long bytes) {
                        ui.post(() -> status.setText(String.format(Locale.US,
                                "download %d/%d  ·  %.1f MB", done, total, bytes / 1048576.0)));
                    }

                    @Override
                    public void onSegmentError(int index, String msg) {
                        ui.post(() -> details.setText("seg " + index + ": " + msg));
                    }
                });

                // WebView sniff ne jo headers dekhe wo downloader ko do
                dl.extraHeaders.putAll(NetSniffer.sniffedAuthHeaders());

                Downloader.Result r = dl.run(t);
                lastResult = r;

                if (r.ok()) {
                    say("✓ download ho gaya — " + (r.bytes / 1048576.0) + " MB, Gallery me save kar rahe hain…");
                    boolean saved = saveToGallery(r);
                    ui.post(() -> {
                        saveBtn.setEnabled(true);
                        if (saved) {
                            say("✓ Gallery me save: Movies/LectureSaver/lecture-….ts");
                            details.setText("segments: " + r.segments + " · fail: " + r.failed
                                    + "\nfile: " + r.file.getAbsolutePath());
                            offerPlay(r);
                        } else {
                            say("download ho gaya par Gallery save fail — file: " + r.file);
                        }
                    });
                } else if (r.keyMissing) {
                    ui.post(() -> {
                        saveBtn.setEnabled(true);
                        say("✗ AES key nahi mili (" + r.keyHost + ")");
                        StringBuilder sb = new StringBuilder();
                        sb.append("segments cache me safe hain — key milte hi file turant ban jayegi.\n\n");
                        sb.append("Key endpoint ne kaha nahi:\n");
                        for (String s : r.keyDiag) sb.append("  · ").append(s).append("\n");
                        sb.append("\nAb \"⚡ Key sniff\" dabayein: page khulega, lecture play hoga, "
                                + "player ke asli headers/cookies pakde jayenge. Uske baad dobara Download dabayein.");
                        details.setText(sb.toString());
                    });
                } else {
                    ui.post(() -> {
                        saveBtn.setEnabled(true);
                        say("kuch segments nahi mile — " + (r.failed > 0 ? "fail=" + r.failed : "network dekhein"));
                    });
                }
            } catch (Exception e) {
                ui.post(() -> {
                    saveBtn.setEnabled(true);
                    say("✗ " + (e.getMessage() == null ? e.toString() : e.getMessage()));
                });
            }
        });
    }

    private void offerPlay(Downloader.Result r) {
        new AlertDialog.Builder(this)
                .setTitle("Save ho gaya")
                .setMessage("Abhi dekhna hai?")
                .setPositiveButton("Play", (x, y) -> {
                    try {
                        Uri u = androidx.core.content.FileProvider.getUriForFile(this,
                                getPackageName() + ".files", r.file);
                        Intent i = new Intent(Intent.ACTION_VIEW);
                        i.setDataAndType(u, "video/mp2t");
                        i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                        startActivity(i);
                    } catch (Exception e) {
                        toast("player app nahi mila — Gallery se dekhein");
                    }
                })
                .setNegativeButton("Baad me", null)
                .show();
    }

    /** File ko MediaStore (Movies/LectureSaver) me daalo — direct Gallery me dikhegi. */
    private boolean saveToGallery(Downloader.Result r) {
        String name = "lecture-" + new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(new Date()) + ".ts";
        try {
            File src = r.file;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ContentValues cv = new ContentValues();
                cv.put(MediaStore.Video.Media.DISPLAY_NAME, name);
                cv.put(MediaStore.Video.Media.MIME_TYPE, "video/mp2t");
                cv.put(MediaStore.Video.Media.RELATIVE_PATH,
                        Environment.DIRECTORY_MOVIES + "/LectureSaver");
                cv.put(MediaStore.Video.Media.IS_PENDING, 1);
                Uri uri = getContentResolver().insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, cv);
                if (uri == null) return false;
                OutputStream out = getContentResolver().openOutputStream(uri, "w");
                if (out == null) return false;
                out.write(Downloader.readAll(src));
                out.flush();
                out.close();
                cv.clear();
                cv.put(MediaStore.Video.Media.IS_PENDING, 0);
                getContentResolver().update(uri, cv, null, null);
                return true;
            }
            File dir = new File(Environment.getExternalStoragePublicDirectory(
                    Environment.DIRECTORY_MOVIES), "LectureSaver");
            if (!dir.exists() && !dir.mkdirs()) return false;
            File dest = new File(dir, name);
            OutputStream out = new java.io.FileOutputStream(dest);
            out.write(Downloader.readAll(src));
            out.flush();
            out.close();
            sendBroadcast(new Intent(Intent.ACTION_MEDIA_SCANNER_SCAN_FILE, Uri.fromFile(dest)));
            return true;
        } catch (Exception e) {
            say("gallery save fail: " + e.getMessage());
            return false;
        }
    }

    /* ----------------------------------------------------------------- sniff -- */

    /**
     * "Key sniff": page WebView me kholo, recorder JS inject karo, aur player ki
     * asli requests ke headers NetSniffer se pakdo. Lecture chalna shuru ho jaye
     * to 35s baad khud report dikhta hai.
     */
    private void sniff() {
        if (sniffing) {
            toast("sniff pehle se chal raha hai");
            return;
        }
        String raw = urlBox.getText().toString().trim();
        if (raw.isEmpty()) {
            toast("pehle link paste karein");
            return;
        }
        sniffing = true;
        NetSniffer.clear();
        say("page khul raha hai — lecture chalne dein (35s)…");

        Intent i = new Intent(this, SniffActivity.class);
        i.putExtra(SniffActivity.EXTRA_URL, raw);
        startActivity(i);
        ui.postDelayed(this::sniffReport, 36_000);
    }

    private void sniffReport() {
        sniffing = false;
        java.util.List<NetSniffer.Hit> hits = NetSniffer.hits();
        java.util.List<NetSniffer.Hit> keys = NetSniffer.keyHits();
        StringBuilder sb = new StringBuilder();
        sb.append("sniff: ").append(hits.size()).append(" requests pakde\n");
        sb.append("key requests: ").append(keys.size()).append("\n\n");
        for (NetSniffer.Hit h : keys) {
            sb.append("KEY ").append(h.host).append(" -> ").append(h.status).append("\n");
            for (java.util.Map.Entry<String, String> e : h.requestHeaders.entrySet()) {
                sb.append("   ").append(e.getKey()).append(": ").append(e.getValue()).append("\n");
            }
        }
        if (keys.isEmpty()) {
            sb.append("(player ne key maangi hi nahi dikhi — ya video chala nahi)\n");
        }
        java.util.Map<String, String> auth = NetSniffer.sniffedAuthHeaders();
        sb.append("\nheaders: ").append(auth.size()).append("\n");
        for (java.util.Map.Entry<String, String> e : auth.entrySet()) {
            if (!e.getKey().equalsIgnoreCase("Cookie")) {
                sb.append("   ").append(e.getKey()).append(": ")
                        .append(e.getValue().length() > 60 ? e.getValue().substring(0, 60) + "…" : e.getValue())
                        .append("\n");
            }
        }
        details.setText(sb.toString());
        say("sniff done — ab Download dabayein");
    }

    private void openPage() {
        String raw = urlBox.getText().toString().trim();
        Intent i = new Intent(this, SniffActivity.class);
        i.putExtra(SniffActivity.EXTRA_URL, raw.isEmpty() ? DEFAULT_URL : raw);
        startActivity(i);
    }

    private void askLegacyStorage() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q
                && checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, 7);
        }
    }

    @Override
    protected void onDestroy() {
        ui.removeCallbacksAndMessages(null);
        bg.shutdownNow();
        super.onDestroy();
    }

    /** Debug: JSON banane ke liye (crash reports me kaam aata hai). */
    static String json(String k, String v) {
        try {
            return new JSONObject().put(k, v).toString();
        } catch (Exception e) {
            return "{}";
        }
    }
}
