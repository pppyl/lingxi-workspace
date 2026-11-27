package cn.pppyl.lingxiworkspace;

import android.app.*;
import android.content.*;
import android.net.Uri;
import android.os.*;
import android.provider.Settings;
import android.webkit.*;
import android.widget.*;
import android.view.*;
import android.Manifest;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

public final class MainActivity extends Activity {
    private static final int OPEN_ACTIVITY_ZIP = 2, SAVE_ACTIVITY_ZIP = 3;
    private WebView web;
    private TextView status;
    private ValueCallback<Uri[]> packageFile;
    private String packageExport;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable awaitReady = new Runnable() {
        public void run() {
            if (!WorkspaceService.error.isEmpty()) { status.setText(WorkspaceService.error); return; }
            if (WorkspaceService.url.isEmpty()) { handler.postDelayed(this, 250); return; }
            CookieManager.getInstance().setCookie(WorkspaceService.url, "lingxi_mobile=" + WorkspaceService.token + "; Path=/; HttpOnly; SameSite=Strict", value -> web.loadUrl(WorkspaceService.url));
        }
    };
    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        getWindow().setStatusBarColor(0xff17263b);
        getWindow().setNavigationBarColor(0xfff4f7fc);
        LinearLayout root = new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL); root.setFitsSystemWindows(true);
        LinearLayout toolbar = new LinearLayout(this); toolbar.setGravity(Gravity.CENTER_VERTICAL);
        TextView title = new TextView(this); title.setText("本地工具台"); title.setTextSize(18); title.setPadding(20, 12, 0, 12);
        toolbar.addView(title, new LinearLayout.LayoutParams(0, -2, 1));
        Button menu = new Button(this); menu.setText("⋮"); menu.setContentDescription("应用菜单");
        toolbar.addView(menu, new LinearLayout.LayoutParams((int)(48 * getResources().getDisplayMetrics().density), -2));
        menu.setOnClickListener(v -> { PopupMenu popup = new PopupMenu(this, v); onCreateOptionsMenu(popup.getMenu()); popup.setOnMenuItemClickListener(this::onOptionsItemSelected); popup.show(); });
        root.addView(toolbar);
        status = new TextView(this); status.setText("正在启动手机本机服务…"); status.setPadding(24, 32, 24, 32); root.addView(status);
        web = new WebView(this); web.setBackgroundColor(0xfff4f7fc);
        root.addView(web, new LinearLayout.LayoutParams(-1, 0, 1)); setContentView(root);
        web.getSettings().setJavaScriptEnabled(true); web.getSettings().setDomStorageEnabled(true);
        web.getSettings().setAllowFileAccess(false); web.getSettings().setAllowContentAccess(false);
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (packageFile != null) packageFile.onReceiveValue(null);
                packageFile = callback;
                Intent pick = new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("application/zip");
                startActivityForResult(pick, OPEN_ACTIVITY_ZIP);
                return true;
            }
        });
        web.setDownloadListener((url, agent, disposition, mime, length) -> {
            Uri source = Uri.parse(url);
            Uri local = Uri.parse(WorkspaceService.url);
            if (!"http".equals(source.getScheme()) || !"127.0.0.1".equals(source.getHost())
                    || source.getPort() != local.getPort() || !"application/zip".equals(mime)
                    || !source.getPath().matches("/api/platforms/[a-z][a-z0-9-]{0,63}/package")) return;
            packageExport = url;
            Intent save = new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                    .setType("application/zip").putExtra(Intent.EXTRA_TITLE, URLUtil.guessFileName(url, disposition, mime));
            startActivityForResult(save, SAVE_ACTIVITY_ZIP);
        });
        web.setWebViewClient(new WebViewClient() {
            @Override public void onPageFinished(WebView view, String url) { status.setVisibility(View.GONE); }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) { status.setText("页面加载失败，请从菜单重新加载。"); status.setVisibility(View.VISIBLE); }
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if ("127.0.0.1".equals(uri.getHost())) return false;
                if ("https".equals(uri.getScheme()) || "http".equals(uri.getScheme())) {
                    startActivity(new Intent(Intent.ACTION_VIEW, uri));
                } else if ("tel".equals(uri.getScheme()) && uri.getSchemeSpecificPart().matches("1[3-9][0-9]{9}")) {
                    startActivity(new Intent(Intent.ACTION_DIAL, uri));
                }
                return true;
            }
        });
        if (Build.VERSION.SDK_INT >= 33) requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        Intent service = new Intent(this, WorkspaceService.class);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(service); else startService(service);
        handler.post(awaitReady);
    }
    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        Uri file = result == RESULT_OK && data != null ? data.getData() : null;
        if (request == OPEN_ACTIVITY_ZIP && packageFile != null) {
            packageFile.onReceiveValue(file != null && "content".equals(file.getScheme()) ? new Uri[]{file} : null);
            packageFile = null;
        } else if (request == SAVE_ACTIVITY_ZIP) {
            String source = packageExport;
            packageExport = null;
            if (file != null && source != null) savePackage(source, file, WorkspaceService.token);
        }
    }

    private void savePackage(String source, Uri destination, String token) {
        new Thread(() -> {
            String message = "活动包已导出";
            try {
                HttpURLConnection connection = (HttpURLConnection) new URL(source).openConnection();
                connection.setInstanceFollowRedirects(false);
                connection.setConnectTimeout(10000);
                connection.setReadTimeout(20000);
                connection.setRequestProperty("Cookie", "lingxi_mobile=" + token);
                try {
                    if (connection.getResponseCode() != 200 || !"application/zip".equals(connection.getContentType())) {
                        throw new IOException("Invalid activity package response");
                    }
                    try (InputStream input = connection.getInputStream();
                         OutputStream output = getContentResolver().openOutputStream(destination, "wt")) {
                        if (output == null) throw new IOException("Package destination is unavailable");
                        byte[] buffer = new byte[8192];
                        int size;
                        while ((size = input.read(buffer)) != -1) output.write(buffer, 0, size);
                    }
                } finally {
                    connection.disconnect();
                }
            } catch (IOException error) {
                message = "活动包导出失败，请重试";
            }
            String result = message;
            runOnUiThread(() -> Toast.makeText(this, result, Toast.LENGTH_SHORT).show());
        }, "activity-package-export").start();
    }
    @Override public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, 1, 0, "重新加载"); menu.add(0, 2, 1, "后台运行说明"); menu.add(0, 3, 2, "停止并退出"); return true;
    }
    @Override public boolean onOptionsItemSelected(MenuItem item) {
        if (item.getItemId() == 1) web.reload();
        if (item.getItemId() == 2) new AlertDialog.Builder(this).setTitle("手机独立运行")
                .setMessage("电脑关闭后仍可使用。定时任务需要保持后台服务运行；请在系统设置中允许本应用后台运行，并关闭电池限制。若手机关机、断网或系统强制停止，任务不能准时执行。启动后会补查当日签到。")
                .setPositiveButton("应用设置", (d, w) -> startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:"+getPackageName())))).setNegativeButton("知道了", null).show();
        if (item.getItemId() == 3) { stopService(new Intent(this, WorkspaceService.class)); finish(); }
        return true;
    }
    @Override public void onBackPressed() { if (web.canGoBack()) web.goBack(); else moveTaskToBack(true); }
    @Override protected void onDestroy() {
        handler.removeCallbacks(awaitReady);
        if (packageFile != null) packageFile.onReceiveValue(null);
        web.destroy();
        super.onDestroy();
    }
}
