package cn.pppyl.lingxiworkspace;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.webkit.*;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.json.JSONTokener;

/** Obtain the official page's anonymous device identifier; no account cookie enters this WebView. */
public final class ActivityRiskBridge {
    private static Context context;
    private static final Handler main = new Handler(Looper.getMainLooper());
    public static void initialize(Context value) { context = value.getApplicationContext(); }
    public static synchronized String getIdentifier(String userAgent) throws Exception {
        CountDownLatch done = new CountDownLatch(1);
        String[] result = {""};
        WebView[] holder = {null};
        long deadline = android.os.SystemClock.uptimeMillis() + 25000;
        main.post(() -> {
            WebView view = new WebView(context); holder[0] = view;
            view.getSettings().setJavaScriptEnabled(true); view.getSettings().setDomStorageEnabled(true);
            view.getSettings().setUserAgentString(userAgent);
            view.getSettings().setAllowFileAccess(false); view.getSettings().setAllowContentAccess(false);
            view.setWebViewClient(new WebViewClient() {
                @Override public void onPageFinished(WebView web, String url) {
                    Runnable check = new Runnable() {
                        public void run() {
                            if (done.getCount() == 0) return;
                            web.evaluateJavascript("typeof window.__constId==='string'?window.__constId:''", encoded -> {
                                try { Object value = new JSONTokener(encoded).nextValue(); if (value instanceof String) result[0] = (String) value; }
                                catch (org.json.JSONException failure) { done.countDown(); return; }
                                if (!result[0].isEmpty() || android.os.SystemClock.uptimeMillis() >= deadline) done.countDown();
                                else main.postDelayed(this, 200);
                            });
                        }
                    };
                    check.run();
                }
            });
            view.loadUrl("https://wx.10086.cn/hlwyxhdhub/act-wedrecharge/index.html");
        });
        done.await(30, TimeUnit.SECONDS);
        done.countDown();
        main.post(() -> { if (holder[0] != null) holder[0].destroy(); });
        return result[0];
    }
}
