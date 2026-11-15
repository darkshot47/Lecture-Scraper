package org.studyratna.saver;

import android.app.Activity;
import android.os.Bundle;
import android.view.Gravity;
import android.view.ViewGroup;
import android.widget.FrameLayout;
import android.widget.TextView;
import android.widget.Toast;

import androidx.annotation.OptIn;
import androidx.media3.common.MediaItem;
import androidx.media3.common.util.UnstableApi;
import androidx.media3.datasource.DefaultHttpDataSource;
import androidx.media3.datasource.okhttp.OkHttpDataSource;
import androidx.media3.exoplayer.ExoPlayer;
import androidx.media3.exoplayer.hls.HlsMediaSource;
import androidx.media3.exoplayer.source.MediaSource;
import androidx.media3.ui.PlayerView;

/**
 * PlayerActivity — lecture seedha app ke andar chalta hai (ExoPlayer).
 *
 * "app se direct dekh bhi sakte ho": yahi wo screen hai. ExoPlayer HLS bhi
 * samajhta hai (master playlist + AES-128), aur headers hum khud dete hain:
 *   - token ki CloudFront cookie
 *   - WebView sniff se mile headers (Referer/Origin/Authorization/client-id)
 *
 * Isse player ke liye browser ki cross-origin dikkat khatam.
 */
@OptIn(markerClass = UnstableApi.class)
public class PlayerActivity extends Activity {

    public static final String EXTRA_URL = "url";
    public static final String EXTRA_COOKIE = "cookie";

    private ExoPlayer player;
    private PlayerView view;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        String url = getIntent().getStringExtra(EXTRA_URL);
        String cookie = getIntent().getStringExtra(EXTRA_COOKIE);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(0xFF000000);

        view = new PlayerView(this);
        view.setUseController(true);
        root.addView(view, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        TextView fail = new TextView(this);
        fail.setTextColor(0xFFFF6B6B);
        fail.setTextSize(13f);
        fail.setPadding(24, 24, 24, 24);
        fail.setGravity(Gravity.BOTTOM);
        fail.setVisibility(TextView.GONE);
        root.addView(fail, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM));

        setContentView(root);

        if (url == null || url.isEmpty()) {
            toast("video URL nahi mila");
            finish();
            return;
        }

        player = new ExoPlayer.Builder(this).build();
        view.setPlayer(player);

        // OkHttp data source: HTTP/1.1 + Chrome headers -> Cloudflare 502 nahi
        final okhttp3.OkHttpClient ok = new okhttp3.OkHttpClient.Builder()
                .connectTimeout(20, java.util.concurrent.TimeUnit.SECONDS)
                .readTimeout(40, java.util.concurrent.TimeUnit.SECONDS)
                .followRedirects(true)
                .protocols(java.util.Collections.singletonList(okhttp3.Protocol.HTTP_1_1))
                .build();
        OkHttpDataSource.Factory http = new OkHttpDataSource.Factory(ok)
                .setUserAgent(Http.UA)
                .setDefaultRequestProperties(chromeHeaders());

        // ---- CDN (CloudFront) par jao: Cloudflare 502 yahan nahi lagta ----
        final String videoKey = Origin.videoKey(url);
        String cdn = Origin.cdnUrl(url);
        String playUrl = url;
        String cdnCookie = "";

        if (cdn != null) {
            cdnCookie = Origin.cookieFor(cookie == null ? "" : cookie, url, cdn);
            // CDN reachable hai? (HEAD) — nahi to player URL
            if (Origin.hasCloudFrontCookie(cdnCookie)) {
                playUrl = cdn;
            }
            fail.setText("CDN: " + (playUrl.equals(cdn) ? "CloudFront ✓" : "player (fallback)"));
        }
        final String fPlayUrl = playUrl;

        String allCookies = cdnCookie.isEmpty()
                ? Origin.cookieFor(cookie == null ? "" : cookie, url)
                : cdnCookie;
        if (!allCookies.isEmpty()) {
            http.setDefaultRequestProperties(java.util.Collections.singletonMap("Cookie", allCookies));
        }
        fail.setVisibility(TextView.GONE);

        MediaSource src = new HlsMediaSource.Factory(http)
                .setAllowChunklessPreparation(true)
                .createMediaSource(MediaItem.fromUri(fPlayUrl));

        player.setMediaSource(src);
        player.prepare();
        player.setPlayWhenReady(true);

        player.addListener(new androidx.media3.common.Player.Listener() {
            private boolean retried = false;

            @Override
            public void onPlayerError(androidx.media3.common.PlaybackException error) {
                fail.setVisibility(TextView.VISIBLE);
                // CDN fail -> player URL par ek retry (aur ulta)
                if (!retried) {
                    retried = true;
                    String other = fPlayUrl.equals(url) ? (cdn == null ? url : cdn) : url;
                    String c = Origin.cookieFor(cookie == null ? "" : cookie, other);
                    OkHttpDataSource.Factory h2 = new OkHttpDataSource.Factory(ok)
                            .setUserAgent(Http.UA)
                            .setDefaultRequestProperties(chromeHeaders());
                    if (!c.isEmpty()) {
                        h2.setDefaultRequestProperties(java.util.Collections.singletonMap("Cookie", c));
                    }
                    fail.setText("pehla raasta fail (" + error.getErrorCodeName()
                            + "), doosra try: " + Origin.host(other));
                    player.setMediaSource(new HlsMediaSource.Factory(h2)
                            .createMediaSource(MediaItem.fromUri(other)));
                    player.prepare();
                    player.setPlayWhenReady(true);
                    return;
                }
                fail.setText("playback error: " + error.getErrorCodeName()
                        + "\n" + (error.getMessage() == null ? "" : error.getMessage())
                        + "\n\nTry: 1) 'Page kholo' (WebView me player)  2) REC na chale to 'Page se download'"
                        + "\nCDN: " + Origin.host(fPlayUrl));
            }
        });
    }

    /** Wahi browser headers jo Http/OkHttp bhejta hai (Cloudflare bypass). */
    private static java.util.Map<String, String> chromeHeaders() {
        java.util.Map<String, String> h = new java.util.LinkedHashMap<>();
        h.put("Accept", "*/*");
        h.put("Accept-Language", "en-IN,en;q=0.9,hi;q=0.8");
        h.put("Sec-Fetch-Dest", "empty");
        h.put("Sec-Fetch-Mode", "cors");
        h.put("Sec-Fetch-Site", "cross-site");
        h.put("Sec-CH-UA", "\"Chromium\";v=\"130\", \"Android WebView\";v=\"130\"");
        h.put("Sec-CH-UA-Mobile", "?1");
        h.put("Sec-CH-UA-Platform", "\"Android\"");
        h.put("Referer", "https://player.studyratna.org/");
        h.put("Origin", "https://player.studyratna.org");
        return h;
    }

    private void toast(String m) {
        Toast.makeText(this, m, Toast.LENGTH_SHORT).show();
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (player != null) player.pause();
    }

    @Override
    protected void onDestroy() {
        if (player != null) {
            player.release();
            player = null;
        }
        super.onDestroy();
    }
}
