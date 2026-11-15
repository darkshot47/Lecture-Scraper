package org.studyratna.saver;

import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.MediaStore;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStream;

/**
 * GalleryWriter — kisi bhi bytes ko Gallery (Movies/LectureSaver) me daalta hai.
 * Android 10+ par MediaStore (IS_PENDING), purane par direct file + media scan.
 */
public final class GalleryWriter {

    public static final String FOLDER = "LectureSaver";

    private GalleryWriter() {
    }

    public static boolean save(Context ctx, byte[] data, String name, String mime) {
        if (data == null || data.length == 0) return false;
        if (mime == null || mime.isEmpty()) mime = "video/mp2t";
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ContentValues cv = new ContentValues();
                cv.put(MediaStore.Video.Media.DISPLAY_NAME, name);
                cv.put(MediaStore.Video.Media.MIME_TYPE, mime);
                cv.put(MediaStore.Video.Media.RELATIVE_PATH,
                        Environment.DIRECTORY_MOVIES + "/" + FOLDER);
                cv.put(MediaStore.Video.Media.IS_PENDING, 1);
                Uri uri = ctx.getContentResolver().insert(
                        MediaStore.Video.Media.EXTERNAL_CONTENT_URI, cv);
                if (uri == null) return false;
                OutputStream out = ctx.getContentResolver().openOutputStream(uri, "w");
                if (out == null) return false;
                out.write(data);
                out.flush();
                out.close();
                cv.clear();
                cv.put(MediaStore.Video.Media.IS_PENDING, 0);
                ctx.getContentResolver().update(uri, cv, null, null);
                return true;
            }
            File dir = new File(Environment.getExternalStoragePublicDirectory(
                    Environment.DIRECTORY_MOVIES), FOLDER);
            if (!dir.exists() && !dir.mkdirs()) return false;
            File dest = new File(dir, name);
            FileOutputStream fos = new FileOutputStream(dest);
            fos.write(data);
            fos.flush();
            fos.close();
            ctx.sendBroadcast(new Intent(Intent.ACTION_MEDIA_SCANNER_SCAN_FILE,
                    Uri.fromFile(dest)));
            return true;
        } catch (Exception e) {
            return false;
        }
    }
}
