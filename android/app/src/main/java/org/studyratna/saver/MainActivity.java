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
import android.view.View;
import android.view.ViewGroup;
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

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Lecture Saver - token URL se lecture ko Gallery me save karta hai.
 *
 * Flow:
 *   1. User token URL paste karta hai (EditText).
 *   2. URL WebView me khulta hai; page load se pehle inject_webview.js chalta hai
 *      jo hls.js ke decrypted media (appendBuffer) aur MediaRecorder (video/mp4)
 *      dono ko capture karta hai.
 *   3. User play karta hai; app status dikhata hai (Kitna MB capture hua).
 *   4. Save dabate hi MSE bytes ya recorder ka MP4 MediaStore me
 *      Movies/LectureSaver me chala jata hai -> Gallery me turant dikhta hai.
 *
 * Koi DRM/geo/paywall bypass nahi: jo user apne browser me dekh sakta hai, wahi
 * save hota hai. App sirf user ke apne device par kaam karta hai.
 */
public class MainActivity extends Activity {

    private static final String ASSET_JS = "inject_webview.js";
    private static final String DEFAULT_URL = "https://app.studyratna.org/";

    private WebView web;
    private EditText urlBox;
    private TextView status;
    private Button saveBtn;
    private Button recordBtn;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private String recordedBase64 = null;   // recorder ke chunks (partial mp4/webm)
    private String mseKind = "unknown";
    private long mseBytes = 0;
    private String recMime = "";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (12 * getResources().getDisplayMetrics().density);
        root.setPadding(pad, pad, pad, pad);

        TextView title = new TextView(this);
        title.setText("Lecture Saver — token link paste karein, lecture play karein, Save dabayein");
        title.setTextSize(14f);
        root.addView(title);

        urlBox = new EditText(this);
        urlBox.setSingleLine(false);
        urlBox.setHint("https://app.studyratna.org/?playerToken=...");
        urlBox.setTextSize(12f);
        root.addView(urlBox, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);

        Button openBtn = new Button(this);
        openBtn.setText("Open");
        openBtn.setOnClickListener(v -> openUrl());
        row.addView(openBtn);

        saveBtn = new Button(this);
        saveBtn.setText("Save to Gallery");
        saveBtn.setEnabled(false);
        saveBtn.setOnClickListener(v -> saveToGallery());
        row.addView(saveBtn);

        recordBtn = new Button(this);
        recordBtn.setText("Stop rec");
        recordBtn.setOnClickListener(v -> stopRecorder());
        row.addView(recordBtn);

        root.addView(row);

        status = new TextView(this);
        status.setTextSize(12f);
        status.setText("status: waiting");
        root.addView(status);

        web = new WebView(this);
        root.addView(web, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        setContentView(root);

        requestNotificationFreePermissions();

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        s.setUserAgentString(s.getUserAgentString() + " LectureSaver/1.0");
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        }

        web.setWebViewClient(new WebViewClient());
        web.setWebChromeClient(new WebChromeClient());
        web.addJavascriptInterface(new Bridge(), "AndroidBridge");

        urlBox.setText(getIntent().getStringExtra("url") != null
                ? getIntent().getStringExtra("url") : DEFAULT_URL);

        ui.postDelayed(statusPoll, 1500);
    }

    /** Kuch permissions ki zaroorat nahi (MediaStore on Q+), par purane Android ke liye. */
    private void requestNotificationFreePermissions() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            if (checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                    != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, 7);
            }
        }
    }

    private void openUrl() {
        String url = urlBox.getText().toString().trim();
        if (url.isEmpty() || !url.startsWith("http")) {
            Toast.makeText(this, "Poora https://... link paste karein", Toast.LENGTH_LONG).show();
            return;
        }
        web.loadUrl(url);
        status.setText("opening... lecture play karein, phir Save dabayein");
        saveBtn.setEnabled(true);
    }

    /** Page ke andar recorder inject karein (har load par). */
    private void injectRecorder() {
        try {
            String js = readAsset(ASSET_JS);
            web.evaluateJavascript(js, null);
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

    private final Runnable statusPoll = new Runnable() {
        @Override
        public void run() {
            web.evaluateJavascript(
                    "(function(){try{return window.__lsStatus ? window.__lsStatus() : null;}catch(e){return null;}})()",
                    value -> {
                        if (value == null || "null".equals(value)) return;
                        String raw = value;
                        if (raw.startsWith("\"") && raw.endsWith("\"")) {
                            raw = raw.substring(1, raw.length() - 1).replace("\\\"", "\"");
                        }
                        try {
                            JSONObject o = new JSONObject(raw);
                            mseBytes = o.optLong("mseBytes");
                            mseKind = o.optString("mseKind", "unknown");
                            recMime = o.optString("recMime", "");
                            long recBytes = o.optLong("recBytes");
                            double cur = o.optDouble("current", 0);
                            double dur = o.optDouble("duration", 0);
                            status.setText(String.format(Locale.US,
                                    "capture: %.1f MB (%s)  |  rec: %.1f MB %s  |  %.0fs%s",
                                    mseBytes / 1048576.0, mseKind, recBytes / 1048576.0,
                                    recMime.contains("mp4") ? "(mp4)" : "",
                                    cur, dur > 0 ? String.format(Locale.US, " / %.0fs", dur) : ""));
                        } catch (Exception ignored) {
                        }
                    });
            ui.postDelayed(this, 2000);
        }
    };

    private void stopRecorder() {
        web.evaluateJavascript(
                "(function(){try{window.__lsRecStop();return 1;}catch(e){return 0;}})()", null);
        Toast.makeText(this, "recorder stop — ab Save dabayein", Toast.LENGTH_SHORT).show();
    }

    /** MSE chunks (lossless) -> file -> Gallery. */
    private void saveToGallery() {
        status.setText("capture nikaal rahe hain...");
        web.evaluateJavascript(
                "(function(){try{return window.__lsDumpMse ? window.__lsDumpMse() : '';}catch(e){return '';}})()",
                value -> {
                    String b64 = unquoteJs(value);
                    if (b64 != null && b64.length() > 1000) {
                        boolean mp4 = "mp4".equals(mseKind);
                        String name = "lecture-" + stamp() + (mp4 ? ".mp4" : ".ts");
                        String mime = mp4 ? "video/mp4" : "video/mp2t";
                        if (writeToGallery(Base64.decode(b64, Base64.DEFAULT), name, mime)) {
                            status.setText("Gallery me save ho gaya: Movies/LectureSaver/" + name);
                            Toast.makeText(this, "Saved: " + name, Toast.LENGTH_LONG).show();
                            return;
                        }
                    }
                    if (recordedBase64 != null && recordedBase64.length() > 1000) {
                        boolean mp4 = recMime.contains("mp4");
                        String name = "lecture-" + stamp() + (mp4 ? ".mp4" : ".webm");
                        String mime = mp4 ? "video/mp4" : "video/webm";
                        if (writeToGallery(Base64.decode(recordedBase64, Base64.DEFAULT), name, mime)) {
                            status.setText("Recorder se Gallery me save: " + name);
                            Toast.makeText(this, "Saved: " + name, Toast.LENGTH_LONG).show();
                            return;
                        }
                    }
                    status.setText("kuch capture nahi hua — lecture play karke 20-30s rukein, phir Save");
                    Toast.makeText(MainActivity.this,
                            "Pehle lecture play karein (data tabhi aata hai)", Toast.LENGTH_LONG).show();
                });
    }

    private String unquoteJs(String value) {
        if (value == null) return null;
        String v = value.trim();
        if (v.startsWith("\"") && v.endsWith("\"")) {
            try {
                return new JSONObject("{\"v\":" + v + "}").getString("v");
            } catch (Exception e) {
                return v.substring(1, v.length() - 1);
            }
        }
        return v;
    }

    /** MediaStore me likho - Gallery/Photos me turant dikhta hai (Android 10+ aur purane dono). */
    private boolean writeToGallery(byte[] data, String fileName, String mime) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ContentValues cv = new ContentValues();
                cv.put(MediaStore.Video.Media.DISPLAY_NAME, fileName);
                cv.put(MediaStore.Video.Media.MIME_TYPE, mime);
                cv.put(MediaStore.Video.Media.RELATIVE_PATH,
                        Environment.DIRECTORY_MOVIES + "/LectureSaver");
                cv.put(MediaStore.Video.Media.IS_PENDING, 1);
                Uri uri = getContentResolver().insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, cv);
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

    /** JS se aane wale messages (recorder chunks + logs + ready). */
    private class Bridge {
        @JavascriptInterface
        public void postMessage(String json) {
            try {
                JSONObject o = new JSONObject(json);
                String type = o.optString("type");
                if ("rec".equals(type)) {
                    recordedBase64 = o.optString("b64");
                } else if ("log".equals(type)) {
                    final String msg = o.optString("msg");
                    ui.post(() -> status.setText(msg));
                } else if ("ready".equals(type)) {
                    ui.post(() -> status.setText("recorder ready — lecture play karein"));
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
