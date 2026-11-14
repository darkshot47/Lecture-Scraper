package org.studyratna.saver;

import android.Manifest;
import android.app.Activity;
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
import android.util.Base64;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.ConsoleMessage;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.BufferedOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Lecture Saver - ek click me lecture Gallery me.
 *
 * UI niyam (aapki complaint ke baad):
 *   - token box 2 line ka, scroll karta hai, buttons ko kabhi dhakta nahi
 *   - buttons fixed height, "folder icon" se full token paste hota hai
 *   - ONE CLICK: "Save to Gallery" -> page khulta hai, player khud play hota hai,
 *     capture khud save hota hai (aur lecture end par bhi auto-save)
 *
 * Ek bhi manual step nahi chahiye: URL box me link paste karke ek button dabao.
 */
public class MainActivity extends Activity {

    private static final String ASSET_JS = "inject_webview.js";
    private static final String DEFAULT_URL = "https://app.studyratna.org/";
    private static final long AUTO_SAVE_MIN_MS = 30_000;   // 30s capture ke baad hi auto-save
    private static final long STARTUP_TIMEOUT_MS = 25_000;
    private static final long END_WAIT_MS = 8_000;

    private WebView web;
    private EditText urlBox;
    private TextView status;
    private Button oneTapBtn;
    private Button stopBtn;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private boolean oneTapMode = false;
    private long oneTapStartedAt = 0;
    private boolean saveStarted = false;
    private volatile long lastMs = 0;

    // chunked save
    private OutputStream streamOut = null;
    private Uri streamUri = null;
    private File streamFile = null;
    private String streamName = null;
    private String streamMime = null;
    private boolean streamIsLegacy = false;
    private long streamBytes = 0;

    // recorder fallback
    private String recordedBase64 = null;
    private String recMime = "";

    private long mseBytes = 0;
    private String mseKind = "unknown";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        final float d = getResources().getDisplayMetrics().density;
        int pad = (int) (12 * d);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, pad, pad, pad);

        TextView title = new TextView(this);
        title.setText("Lecture Saver");
        title.setTextSize(18f);
        title.setTextColor(0xFF111827);
        root.addView(title);

        TextView hint = new TextView(this);
        hint.setText("Token link paste karein, phir neeche wala button dabayein. "
                + "Page khud khulega, lecture khud chalega, video khud Gallery me save hogi.");
        hint.setTextSize(12f);
        hint.setTextColor(0xFF6B7280);
        hint.setPadding(0, (int) (6 * d), 0, (int) (8 * d));
        root.addView(hint);

        // --- token row: chhota box + paste/clear buttons + full-screen edit ---
        LinearLayout urlRow = new LinearLayout(this);
        urlRow.setOrientation(LinearLayout.HORIZONTAL);
        urlRow.setGravity(Gravity.CENTER_VERTICAL);

        urlBox = new EditText(this);
        urlBox.setSingleLine(true);                       // 1 line: buttons kabhi na dhake
        urlBox.setHorizontallyScrolling(true);
        urlBox.setHint("token link paste karein…");
        urlBox.setTextSize(12f);
        LinearLayout.LayoutParams boxLp = new LinearLayout.LayoutParams(0,
                (int) (48 * d), 1f);
        urlRow.addView(urlBox, boxLp);

        urlRow.addView(smallButton("Paste", v -> pasteFromClipboard()));
        urlRow.addView(smallButton("⋯", v -> openUrlDialog()));
        root.addView(urlRow);

        // full URL ek tap me dekhne ke liye (chhote box me lambe token ka pata chale)
        urlBox.setOnLongClickListener(v -> {
            openUrlDialog();
            return true;
        });

        // --- ek bada button: one click save ---
        oneTapBtn = new Button(this);
        oneTapBtn.setText("Save to Gallery (1 click)");
        oneTapBtn.setTextSize(16f);
        oneTapBtn.setAllCaps(false);
        oneTapBtn.setOnClickListener(v -> oneTapSave());
        root.addView(oneTapBtn, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, (int) (56 * d)));

        stopBtn = new Button(this);
        stopBtn.setText("Stop & Save");
        stopBtn.setAllCaps(false);
        stopBtn.setEnabled(false);
        stopBtn.setOnClickListener(v -> {
            if (isCapturing()) {
                saveNow();
            } else {
                toast("abhi capture shuru nahi hua");
            }
        });
        root.addView(stopBtn, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, (int) (44 * d)));

        status = new TextView(this);
        status.setTextSize(12f);
        status.setTextColor(0xFF374151);
        status.setPadding(0, (int) (8 * d), 0, 0);
        status.setText("status: token link paste karein");
        root.addView(status);

        web = new WebView(this);
        root.addView(web, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        setContentView(root);

        askLegacyStoragePermission();

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        }
        s.setUserAgentString(s.getUserAgentString() + " LectureSaver/1.1");

        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                injectRecorder();
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onConsoleMessage(ConsoleMessage cm) {
                status.setText("js: " + cm.message());
                return true;
            }
        });
        web.addJavascriptInterface(new Bridge(), "AndroidBridge");

        String incoming = getIntent().getStringExtra("url");
        urlBox.setText(incoming != null ? incoming : DEFAULT_URL);
        if (incoming != null) {
            ui.postDelayed(this::oneTapSave, 300);
        }

        ui.postDelayed(statusPoll, 1200);
    }

    private Button smallButton(String text, View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextSize(12f);
        b.setPadding(0, 0, 0, 0);
        b.setOnClickListener(l);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                (int) (64 * getResources().getDisplayMetrics().density),
                (int) (44 * getResources().getDisplayMetrics().density));
        lp.setMargins((int) (6 * getResources().getDisplayMetrics().density), 0, 0, 0);
        b.setLayoutParams(lp);
        return b;
    }

    private void askLegacyStoragePermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q
                && checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, 7);
        }
    }

    /* ------------------------------------------------------------------ URL -- */

    private void pasteFromClipboard() {
        try {
            android.content.ClipboardManager cm =
                    (android.content.ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
            if (cm != null && cm.hasPrimaryClip() && cm.getPrimaryClip().getItemCount() > 0) {
                CharSequence txt = cm.getPrimaryClip().getItemAt(0).coerceToText(this);
                if (txt != null && txt.length() > 0) {
                    urlBox.setText(txt.toString().trim());
                    toast("link paste ho gaya");
                    return;
                }
            }
            toast("clipboard khali hai");
        } catch (Exception e) {
            toast("paste fail: " + e.getMessage());
        }
    }

    /** Poora token dekhne/edit karne ke liye full-width dialog. */
    private void openUrlDialog() {
        EditText full = new EditText(this);
        full.setText(urlBox.getText());
        full.setTextSize(12f);
        full.setSingleLine(false);
        full.setMinLines(6);
        full.setGravity(Gravity.TOP | Gravity.START);

        new android.app.AlertDialog.Builder(this)
                .setTitle("Poora token link")
                .setView(full)
                .setPositiveButton("Use", (d, w) -> urlBox.setText(full.getText().toString().trim()))
                .setNegativeButton("Cancel", null)
                .show();
    }

    /* --------------------------------------------------------------- 1-click -- */

    private void oneTapSave() {
        String url = urlBox.getText().toString().trim();
        if (url.isEmpty() || !url.startsWith("http")) {
            toast("poora https://… token link paste karein");
            return;
        }
        saveStarted = false;
        lastMs = 0;
        mseBytes = 0;
        oneTapMode = true;
        oneTapStartedAt = System.currentTimeMillis();
        oneTapBtn.setEnabled(false);
        oneTapBtn.setText("chal raha hai…");
        stopBtn.setEnabled(true);
        status.setText("page khul raha hai…");
        web.loadUrl(url);
    }

    private void injectRecorder() {
        try {
            web.evaluateJavascript(readAsset(ASSET_JS), null);
            ui.postDelayed(() -> web.evaluateJavascript(
                    "(function(){try{window.__lsPoke&&window.__lsPoke();}catch(e){}})()", null),
                    1500);
        } catch (Exception e) {
            status.setText("inject fail: " + e.getMessage());
        }
    }

    private String readAsset(String name) throws Exception {
        InputStream in = getAssets().open(name);
        java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
        in.close();
        return bos.toString("UTF-8");
    }

    /* --------------------------------------------------------------- polling -- */

    private final Runnable statusPoll = new Runnable() {
        @Override
        public void run() {
            web.evaluateJavascript(
                    "(function(){try{return window.__lsStatus?window.__lsStatus():null;}catch(e){return null;}})()",
                    value -> onStatus(value));
            ui.postDelayed(this, 1500);
        }
    };

    private void onStatus(String value) {
        if (value == null || "null".equals(value)) {
            if (oneTapMode && !saveStarted
                    && System.currentTimeMillis() - oneTapStartedAt > STARTUP_TIMEOUT_MS) {
                status.setText("recorder load nahi hua — page reload kar rahe hain…");
                injectRecorder();
                oneTapStartedAt = System.currentTimeMillis();
            }
            return;
        }
        String raw = unquote(value);
        try {
            JSONObject o = new JSONObject(raw);
            mseBytes = o.optLong("mseBytes");
            mseKind = o.optString("mseKind", "unknown");
            recMime = o.optString("recMime", "");
            long recBytes = o.optLong("recBytes");
            double cur = o.optDouble("current", 0);
            double dur = o.optDouble("duration", 0);
            boolean ended = o.optBoolean("ended");
            if (mseBytes > 0) lastMs = System.currentTimeMillis();

            status.setText(String.format(Locale.US,
                    "capture %.1f MB (%s)  •  rec %.1f MB  •  %.0fs%s",
                    mseBytes / 1048576.0, mseKind, recBytes / 1048576.0, cur,
                    dur > 0 ? String.format(Locale.US, " / %.0fs", dur) : ""));

            if (!oneTapMode || saveStarted) return;

            // kuch capture hua? bas.
            long capturedMs = System.currentTimeMillis() - oneTapStartedAt;
            if (mseBytes > 800_000 && capturedMs > AUTO_SAVE_MIN_MS) {
                saveNow();
                return;
            }
            // lecture khatam -> buffer thoda bharne do, phir save
            if (ended && mseBytes > 300_000) {
                ui.postDelayed(() -> {
                    if (!saveStarted) saveNow();
                }, END_WAIT_MS);
            }
        } catch (Exception ignored) {
        }
    }

    private boolean isCapturing() {
        return mseBytes > 50_000 || (recordedBase64 != null && recordedBase64.length() > 1000);
    }

    /* ------------------------------------------------------------------ save -- */

    private void saveNow() {
        if (saveStarted) return;
        if (!isCapturing()) {
            status.setText("capture khali hai — lecture play ho raha hai? "
                    + "agar page par error hai to status bhejein");
            return;
        }
        saveStarted = true;
        oneTapBtn.setEnabled(true);
        oneTapBtn.setText("Save to Gallery (1 click)");

        if (mseBytes >= 50_000) {
            startChunkedSave();
        } else {
            saveRecorderFallback();
        }
    }

    private void startChunkedSave() {
        boolean mp4 = "mp4".equals(mseKind);
        streamName = "lecture-" + stamp() + (mp4 ? ".mp4" : ".ts");
        streamMime = mp4 ? "video/mp4" : "video/mp2t";
        streamBytes = 0;
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ContentValues cv = new ContentValues();
                cv.put(MediaStore.Video.Media.DISPLAY_NAME, streamName);
                cv.put(MediaStore.Video.Media.MIME_TYPE, streamMime);
                cv.put(MediaStore.Video.Media.RELATIVE_PATH,
                        Environment.DIRECTORY_MOVIES + "/LectureSaver");
                cv.put(MediaStore.Video.Media.IS_PENDING, 1);
                streamUri = getContentResolver().insert(
                        MediaStore.Video.Media.EXTERNAL_CONTENT_URI, cv);
                if (streamUri == null) throw new IllegalStateException("MediaStore insert fail");
                streamOut = new BufferedOutputStream(
                        getContentResolver().openOutputStream(streamUri), 1 << 16);
                streamIsLegacy = false;
            } else {
                File dir = new File(Environment.getExternalStoragePublicDirectory(
                        Environment.DIRECTORY_MOVIES), "LectureSaver");
                if (!dir.exists() && !dir.mkdirs()) throw new IllegalStateException("mkdir fail");
                streamFile = new File(dir, streamName);
                streamOut = new BufferedOutputStream(new FileOutputStream(streamFile), 1 << 16);
                streamIsLegacy = true;
            }
        } catch (Exception e) {
            status.setText("save start fail: " + e.getMessage());
            closeStream(false);
            return;
        }
        status.setText("Gallery me save ho raha hai… 0.0 MB");
        web.evaluateJavascript(
                "(function(){try{return window.__lsStartDump?window.__lsStartDump():0;}catch(e){return 0;}})()",
                null);
    }

    private void closeStream(boolean ok) {
        try {
            if (streamOut != null) {
                streamOut.flush();
                streamOut.close();
            }
        } catch (Exception ignored) {
        }
        streamOut = null;
        if (ok) {
            if (!streamIsLegacy && streamUri != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ContentValues cv = new ContentValues();
                cv.put(MediaStore.Video.Media.IS_PENDING, 0);
                getContentResolver().update(streamUri, cv, null, null);
            }
            if (streamIsLegacy && streamFile != null) {
                sendBroadcast(new Intent(Intent.ACTION_MEDIA_SCANNER_SCAN_FILE,
                        Uri.fromFile(streamFile)));
            }
            status.setText("Gallery me save ho gaya: Movies/LectureSaver/" + streamName
                    + String.format(Locale.US, " (%.1f MB)", streamBytes / 1048576.0));
            toast("Saved: " + streamName);
        } else {
            if (streamUri != null) {
                try {
                    getContentResolver().delete(streamUri, null, null);
                } catch (Exception ignored) {
                }
            }
            if (streamFile != null && streamFile.exists()) streamFile.delete();
        }
        streamUri = null;
        streamFile = null;
    }

    private void saveRecorderFallback() {
        status.setText("recorder ka data save kar rahe hain…");
        if (recordedBase64 != null && recordedBase64.length() > 1000) {
            boolean mp4 = recMime.contains("mp4");
            String name = "lecture-" + stamp() + (mp4 ? ".mp4" : ".webm");
            String mime = mp4 ? "video/mp4" : "video/webm";
            if (writeToGallery(Base64.decode(recordedBase64, Base64.DEFAULT), name, mime)) {
                status.setText("Gallery me save: " + name);
                toast("Saved: " + name);
                return;
            }
        }
        status.setText("kuch capture nahi hua — lecture play karke 20-30s rukein");
    }

    private String unquote(String value) {
        if (value == null) return "{}";
        String v = value.trim();
        if (v.startsWith("\"") && v.endsWith("\"")) {
            try {
                return new JSONObject("{\"v\":" + v + "}").getString("v");
            } catch (Exception e) {
                return v.substring(1, v.length() - 1).replace("\\\"", "\"");
            }
        }
        return v;
    }

    private boolean writeToGallery(byte[] data, String fileName, String mime) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ContentValues cv = new ContentValues();
                cv.put(MediaStore.Video.Media.DISPLAY_NAME, fileName);
                cv.put(MediaStore.Video.Media.MIME_TYPE, mime);
                cv.put(MediaStore.Video.Media.RELATIVE_PATH,
                        Environment.DIRECTORY_MOVIES + "/LectureSaver");
                cv.put(MediaStore.Video.Media.IS_PENDING, 1);
                Uri uri = getContentResolver().insert(
                        MediaStore.Video.Media.EXTERNAL_CONTENT_URI, cv);
                if (uri == null) return false;
                OutputStream out = getContentResolver().openOutputStream(uri);
                if (out == null) return false;
                out.write(data);
                out.close();
                cv.clear();
                cv.put(MediaStore.Video.Media.IS_PENDING, 0);
                getContentResolver().update(uri, cv, null, null);
                return true;
            }
            File dir = new File(Environment.getExternalStoragePublicDirectory(
                    Environment.DIRECTORY_MOVIES), "LectureSaver");
            if (!dir.exists() && !dir.mkdirs()) return false;
            File f = new File(dir, fileName);
            FileOutputStream fos = new FileOutputStream(f);
            fos.write(data);
            fos.close();
            sendBroadcast(new Intent(Intent.ACTION_MEDIA_SCANNER_SCAN_FILE, Uri.fromFile(f)));
            return true;
        } catch (Exception e) {
            status.setText("save fail: " + e.getMessage());
            return false;
        }
    }

    private String stamp() {
        return new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(new Date());
    }

    private void toast(String msg) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show();
    }

    /* ---------------------------------------------------------------- bridge -- */

    private class Bridge {
        @JavascriptInterface
        public void postMessage(String json) {
            try {
                JSONObject o = new JSONObject(json);
                String type = o.optString("type");
                if ("mse".equals(type)) {
                    final byte[] bytes = Base64.decode(o.optString("b64"), Base64.DEFAULT);
                    final boolean last = o.optBoolean("last");
                    ui.post(() -> {
                        try {
                            if (streamOut != null) streamOut.write(bytes);
                            streamBytes += bytes.length;
                            status.setText(String.format(Locale.US,
                                    "Gallery me save ho raha hai… %.1f MB",
                                    streamBytes / 1048576.0));
                            if (last) closeStream(true);
                        } catch (Exception e) {
                            status.setText("write fail: " + e.getMessage());
                            closeStream(false);
                        }
                    });
                    return;
                }
                if ("mse-done".equals(type) && !o.optBoolean("ok")) {
                    ui.post(() -> {
                        status.setText("capture khali hai");
                        closeStream(false);
                    });
                    return;
                }
                if ("rec".equals(type)) {
                    recordedBase64 = o.optString("b64");
                    recMime = o.optString("mime", recMime);
                    return;
                }
                if ("log".equals(type)) {
                    final String msg = o.optString("msg");
                    ui.post(() -> status.setText(msg));
                }
            } catch (Exception ignored) {
            }
        }
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        ui.removeCallbacksAndMessages(null);
        if (web != null) web.destroy();
        super.onDestroy();
    }
}
