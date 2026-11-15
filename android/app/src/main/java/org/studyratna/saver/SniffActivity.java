package org.studyratna.saver;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.ViewGroup;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

/**
 * SniffActivity — StudyRatna page WebView me kholta hai.
 *
 * Kaam:
 *   1. page ko normally load karne deta hai (player khud chalta hai)
 *   2. har request par NetSniffer.record() — headers/cookies ikatthe hote hain
 *   3. recorder JS inject karta hai (MSE chunks + MediaRecorder fallback me
 *      page ke andar capture ke liye — agar native download fail ho)
 *   4. jab player key maange (get-hls-key), wo request bhi record hoti hai
 *
 * Isse humein wahi Referer/Origin/Cookie/Authorization mil jaate hain jo
 * browser bhejta hai — bina kisi CORS dikkat ke.
 */
public class SniffActivity extends Activity {

    public static final String EXTRA_URL = "url";

    private WebView web;
    private TextView bar;
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        String url = getIntent() != null ? getIntent().getStringExtra(EXTRA_URL) : null;
        if (url == null || url.isEmpty()) url = "https://app.studyratna.org/";

        FrameLayout root = new FrameLayout(this);

        web = new WebView(this);
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.HORIZONTAL);
        top.setBackgroundColor(0xCC000000);
        top.setGravity(Gravity.CENTER_VERTICAL);

        bar = new TextView(this);
        bar.setTextColor(Color.WHITE);
        bar.setTextSize(11f);
        bar.setPadding(14, 10, 8, 10);
        bar.setText("sniff: 0 requests");
        top.addView(bar, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        top.addView(btn("Close", v -> finish()));
        top.addView(btn("Copy log", v -> copyLog()));

        root.addView(top, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.TOP));

        setContentView(root);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setUserAgentString(s.getUserAgentString() + " LectureSaver/2.0");
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        }
        android.webkit.CookieManager.getInstance().setAcceptCookie(true);
        android.webkit.CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public android.webkit.WebResourceResponse shouldInterceptRequest(
                    WebView view, WebResourceRequest req) {
                NetSniffer.record(view, req);
                return null;   // request ko rokо nahi — jaisi hai waise jaane do
            }

            @Override
            public void onPageFinished(WebView view, String u) {
                NetSniffer.absorbCookiesFrom(u);
                injectRecorder();
            }
        });
        web.setWebChromeClient(new WebChromeClient());
        web.addJavascriptInterface(new RecBridge(), "AndroidBridge");

        web.loadUrl(url);
        ui.postDelayed(this::tick, 1500);
    }

    private Button btn(String text, android.view.View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextSize(10f);
        b.setPadding(10, 0, 10, 0);
        b.setOnClickListener(l);
        b.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                (int) (38 * getResources().getDisplayMetrics().density)));
        return b;
    }

    private void tick() {
        int n = NetSniffer.hits().size();
        int k = NetSniffer.keyHits().size();
        bar.setText("sniff: " + n + " req · key " + k);
        ui.postDelayed(this, 1500);
    }

    private void injectRecorder() {
        try {
            java.io.InputStream in = getAssets().open("inject_webview.js");
            java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
            in.close();
            web.evaluateJavascript(bos.toString("UTF-8"), null);
        } catch (Exception ignored) {
        }
    }

    /** Recorder ka bridge — page se aane wale messages (MSE / rec / log). */
    public class RecBridge {
        @android.webkit.JavascriptInterface
        public void postMessage(String json) {
            if (json == null) return;
            if (json.contains("\"log\"")) {
                try {
                    org.json.JSONObject o = new org.json.JSONObject(json);
                    String msg = o.optString("msg");
                    if (!msg.isEmpty()) {
                        ui.post(() -> bar.setText("js: " + msg));
                    }
                } catch (Exception ignored) {
                }
            }
        }
    }

    private void copyLog() {
        StringBuilder sb = new StringBuilder();
        for (NetSniffer.Hit h : NetSniffer.hits()) {
            sb.append(h.host).append(" ").append(h.status).append("\n");
            sb.append("  ").append(h.url).append("\n");
            for (java.util.Map.Entry<String, String> e : h.requestHeaders.entrySet()) {
                sb.append("  ").append(e.getKey()).append(": ").append(e.getValue()).append("\n");
            }
            sb.append("\n");
        }
        try {
            ClipboardManager cm = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
            if (cm != null) {
                cm.setPrimaryClip(ClipData.newPlainText("sniff-log", sb.toString()));
                Toast.makeText(this, "log copy ho gaya (" + NetSniffer.hits().size() + " requests)",
                        Toast.LENGTH_SHORT).show();
            }
        } catch (Exception e) {
            Toast.makeText(this, "copy fail", Toast.LENGTH_SHORT).show();
        }
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        ui.removeCallbacksAndMessages(null);
        if (web != null) web.destroy();
        super.onDestroy();
    }
}
