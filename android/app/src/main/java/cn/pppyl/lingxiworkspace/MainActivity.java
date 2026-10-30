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

public final class MainActivity extends Activity {
    private WebView web;
    private TextView status;
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
                }
                return true;
            }
        });
        if (Build.VERSION.SDK_INT >= 33) requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        Intent service = new Intent(this, WorkspaceService.class);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(service); else startService(service);
        handler.post(awaitReady);
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
    @Override protected void onDestroy() { handler.removeCallbacks(awaitReady); web.destroy(); super.onDestroy(); }
}
