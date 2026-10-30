package cn.pppyl.lingxiworkspace;

import android.app.*;
import android.content.Intent;
import android.os.*;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import org.json.JSONObject;

public final class WorkspaceService extends Service {
    public static volatile String url = "", token = "", error = "";
    private static volatile boolean starting;
    private PowerManager.WakeLock wake;
    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && "STOP".equals(intent.getAction())) {
            stopSelf(); return START_NOT_STICKY;
        }
        NotificationManager notifications = getSystemService(NotificationManager.class);
        if (Build.VERSION.SDK_INT >= 26) notifications.createNotificationChannel(new NotificationChannel("tasks", "后台任务", NotificationManager.IMPORTANCE_LOW));
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, WorkspaceService.class).setAction("STOP"), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder notice = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, "tasks") : new Notification.Builder(this);
        startForeground(1, notice.setContentTitle("本地工具台运行中").setContentText("账号与任务在本机运行，点击返回工具台")
                .setSmallIcon(android.R.drawable.ic_menu_recent_history).setContentIntent(open).setOngoing(true)
                .addAction(new Notification.Action.Builder(null, "停止后台任务", stop).build()).build());
        if (!starting) {
            starting = true; error = "";
            wake = ((PowerManager) getSystemService(POWER_SERVICE)).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "Lingxi:tasks");
            wake.acquire();
            ActivityRiskBridge.initialize(this);
            new Thread(() -> {
                try {
                    if (!Python.isStarted()) Python.start(new AndroidPlatform(this));
                    JSONObject result = new JSONObject(Python.getInstance().getModule("android_runtime").callAttr("start", getFilesDir().getAbsolutePath()).toString());
                    token = result.getString("token"); url = result.getString("url");
                } catch (Exception failure) {
                    error = "本机服务启动失败，请关闭后重试。";
                    android.util.Log.e("LingxiWorkspace", "Startup failed", failure);
                }
            }, "lingxi-start").start();
        }
        return START_STICKY;
    }
    @Override public IBinder onBind(Intent intent) { return null; }
    @Override public void onDestroy() {
        url = ""; token = ""; starting = false;
        if (wake != null && wake.isHeld()) wake.release();
        stopForeground(true);
        super.onDestroy();
        new Thread(() -> {
            try { if (Python.isStarted()) Python.getInstance().getModule("android_runtime").callAttr("stop"); }
            finally { android.os.Process.killProcess(android.os.Process.myPid()); }
        }, "lingxi-stop").start();
    }
}
