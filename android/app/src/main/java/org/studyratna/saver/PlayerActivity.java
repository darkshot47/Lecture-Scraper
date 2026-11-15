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

        DefaultHttpDataSource.Factory http = new DefaultHttpDataSource.Factory()
                .setUserAgent("Mozilla/5.0 (Linux; Android 13) LectureSaver/2.0")
                .setAllowCrossProtocolRedirects(true)
                .setConnectTimeoutMs(20000)
                .setReadTimeoutMs(30000);

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
                    DefaultHttpDataSource.Factory h2 = new DefaultHttpDataSource.Factory()
                            .setUserAgent("Mozilla/5.0 (Linux; Android 13) LectureSaver/2.2")
                            .setAllowCrossProtocolRedirects(true);
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
