package app.htmlapk.runtime;

import android.Manifest;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ContentValues;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Message;
import android.provider.MediaStore;
import android.util.Base64;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.webkit.GeolocationPermissions;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.Toast;

import androidx.webkit.WebViewAssetLoader;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;

/**
 * Generic app shell: shows assets/www/index.html full screen, served from an https address
 * so storage, clipboard and embedded players (YouTube) behave like on a real website.
 */
public class MainActivity extends Activity {
    static final String HOST = "appassets.androidplatform.net";
    static final String START = "https://" + HOST + "/assets/www/index.html";
    static final int REQ_FILE = 11, REQ_PERM = 12;

    FrameLayout root;
    WebView web;
    WebChromeClient chrome;
    View customView;
    WebChromeClient.CustomViewCallback customCallback;
    ValueCallback<Uri[]> fileCallback;
    PermissionRequest pendingWebPerm;
    String pendingGeoOrigin;
    GeolocationPermissions.Callback pendingGeoCallback;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#000000"));
        web = new WebView(this);
        root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        // Keep content clear of the status bar, navigation bar and keyboard (Android 15 draws edge to edge).
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            int l, t, r, b;
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets s = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.ime() | WindowInsets.Type.displayCutout());
                l = s.left; t = s.top; r = s.right; b = s.bottom;
            } else {
                l = insets.getSystemWindowInsetLeft(); t = insets.getSystemWindowInsetTop();
                r = insets.getSystemWindowInsetRight(); b = insets.getSystemWindowInsetBottom();
            }
            if (customView == null) v.setPadding(l, t, r, b); else v.setPadding(0, 0, 0, 0);
            // The bars are already kept clear here, so the page must not add the same space again
            // through CSS env(safe-area-inset-*): consume the insets so the WebView sees none.
            if (Build.VERSION.SDK_INT >= 30) return WindowInsets.CONSUMED;
            return insets.consumeSystemWindowInsets();
        });

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setSupportMultipleWindows(true);
        s.setJavaScriptCanOpenWindowsAutomatically(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setTextZoom(100);
        web.setBackgroundColor(Color.TRANSPARENT);
        web.addJavascriptInterface(new Bridge(), "HtmlApk");

        final WebViewAssetLoader loader = new WebViewAssetLoader.Builder()
                .setDomain(HOST)
                .addPathHandler("/assets/", new WebViewAssetLoader.AssetsPathHandler(this))
                .build();

        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest req) {
                return loader.shouldInterceptRequest(req.getUrl());
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                if (HOST.equals(u.getHost())) return false;
                if (!req.isForMainFrame()) return false;
                openOutside(u);
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                view.evaluateJavascript(SHIM, null);
            }
        });

        web.setWebChromeClient(chrome = new WebChromeClient() {
            // Links with target=_blank or window.open(): open in the browser or the matching app
            @Override
            public boolean onCreateWindow(WebView view, boolean dialog, boolean user, Message msg) {
                WebView tmp = new WebView(MainActivity.this);
                tmp.setWebViewClient(new WebViewClient() {
                    @Override
                    public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                        Uri u = r.getUrl();
                        if (HOST.equals(u.getHost())) web.loadUrl(u.toString()); else openOutside(u);
                        v.destroy();
                        return true;
                    }
                });
                ((WebView.WebViewTransport) msg.obj).setWebView(tmp);
                msg.sendToTarget();
                return true;
            }

            // Full-screen video (YouTube etc.)
            @Override
            public void onShowCustomView(View view, CustomViewCallback cb) {
                if (customView != null) { cb.onCustomViewHidden(); return; }
                customView = view; customCallback = cb;
                root.addView(view, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
                root.setPadding(0, 0, 0, 0);
                web.setVisibility(View.GONE);
                getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
            }

            @Override
            public void onHideCustomView() {
                if (customView == null) return;
                root.removeView(customView);
                customView = null;
                web.setVisibility(View.VISIBLE);
                getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_VISIBLE);
                root.requestApplyInsets();
                if (customCallback != null) customCallback.onCustomViewHidden();
            }

            // <input type="file">
            @Override
            public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams p) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = cb;
                try {
                    Intent i = p.createIntent();
                    if (p.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE) i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                    startActivityForResult(i, REQ_FILE);
                } catch (ActivityNotFoundException e) {
                    fileCallback = null;
                    return false;
                }
                return true;
            }

            // Camera / microphone for getUserMedia
            @Override
            public void onPermissionRequest(PermissionRequest req) {
                runOnUiThread(() -> {
                    List<String> need = new ArrayList<>();
                    for (String r : req.getResources()) {
                        if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(r)) need.add(Manifest.permission.CAMERA);
                        if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r)) need.add(Manifest.permission.RECORD_AUDIO);
                    }
                    if (missing(need).isEmpty()) { req.grant(req.getResources()); return; }
                    pendingWebPerm = req;
                    requestPermissions(missing(need).toArray(new String[0]), REQ_PERM);
                });
            }

            // navigator.geolocation
            @Override
            public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback cb) {
                List<String> need = new ArrayList<>();
                need.add(Manifest.permission.ACCESS_FINE_LOCATION);
                need.add(Manifest.permission.ACCESS_COARSE_LOCATION);
                if (missing(need).size() < 2) { cb.invoke(origin, true, false); return; }
                pendingGeoOrigin = origin; pendingGeoCallback = cb;
                requestPermissions(need.toArray(new String[0]), REQ_PERM);
            }
        });

        // Plain http(s) downloads go to the system
        web.setDownloadListener((url, ua, cd, mime, len) -> {
            if (url.startsWith("http")) openOutside(Uri.parse(url));
        });

        if (state != null) web.restoreState(state); else web.loadUrl(START);
    }

    List<String> missing(List<String> perms) {
        List<String> out = new ArrayList<>();
        for (String p : perms) if (checkSelfPermission(p) != PackageManager.PERMISSION_GRANTED) out.add(p);
        return out;
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] res) {
        if (code != REQ_PERM) return;
        if (pendingWebPerm != null) {
            boolean ok = true;
            for (int r : res) ok &= r == PackageManager.PERMISSION_GRANTED;
            if (ok) pendingWebPerm.grant(pendingWebPerm.getResources()); else pendingWebPerm.deny();
            pendingWebPerm = null;
        }
        if (pendingGeoCallback != null) {
            boolean ok = false;
            for (int r : res) ok |= r == PackageManager.PERMISSION_GRANTED;
            pendingGeoCallback.invoke(pendingGeoOrigin, ok, false);
            pendingGeoCallback = null;
        }
    }

    @Override
    protected void onActivityResult(int code, int result, Intent data) {
        if (code == REQ_FILE && fileCallback != null) {
            Uri[] picked = null;
            if (result == RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    picked = new Uri[data.getClipData().getItemCount()];
                    for (int i = 0; i < picked.length; i++) picked[i] = data.getClipData().getItemAt(i).getUri();
                } else if (data.getData() != null) picked = new Uri[]{data.getData()};
            }
            fileCallback.onReceiveValue(picked);
            fileCallback = null;
            return;
        }
        super.onActivityResult(code, result, data);
    }

    void openOutside(Uri u) {
        try {
            Intent i = new Intent(Intent.ACTION_VIEW, u);
            i.addCategory(Intent.CATEGORY_BROWSABLE);
            startActivity(i);
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, "No app can open this link", Toast.LENGTH_SHORT).show();
        }
    }

    // Android back button: let the page handle it first, then browser history, then leave
    @Override
    public void onBackPressed() {
        if (customView != null) { chrome.onHideCustomView(); return; }
        web.evaluateJavascript(
                "(function(){try{var e=new Event('backbutton',{cancelable:true});document.dispatchEvent(e);return e.defaultPrevented}catch(x){return false}})()",
                v -> {
                    if ("true".equals(v)) return;
                    if (web.canGoBack()) web.goBack(); else moveTaskToBack(true);
                });
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    protected void onPause() { super.onPause(); web.onPause(); }

    @Override
    protected void onResume() { super.onResume(); web.onResume(); }

    /** Functions the page can call as window.HtmlApk.* */
    class Bridge {
        @JavascriptInterface
        public void share(String title, String text, String url) {
            Intent i = new Intent(Intent.ACTION_SEND);
            i.setType("text/plain");
            String body = (text == null ? "" : text);
            if (url != null && !url.isEmpty()) body = body.isEmpty() ? url : body + "\n" + url;
            i.putExtra(Intent.EXTRA_SUBJECT, title);
            i.putExtra(Intent.EXTRA_TEXT, body);
            startActivity(Intent.createChooser(i, title));
        }

        @JavascriptInterface
        public void setBarColor(String hex) {
            runOnUiThread(() -> {
                try {
                    int c = Color.parseColor(hex);
                    root.setBackgroundColor(c);
                    getWindow().setStatusBarColor(c);
                    getWindow().setNavigationBarColor(c);
                    double lum = (0.299 * Color.red(c) + 0.587 * Color.green(c) + 0.114 * Color.blue(c)) / 255;
                    int flags = getWindow().getDecorView().getSystemUiVisibility();
                    if (lum > 0.6) flags |= View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR; else flags &= ~View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
                    if (Build.VERSION.SDK_INT >= 26) {
                        if (lum > 0.6) flags |= View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR; else flags &= ~View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
                    }
                    getWindow().getDecorView().setSystemUiVisibility(flags);
                } catch (Exception ignored) { }
            });
        }

        /** Saves a file the page generated (blob/data link with download attribute) into Downloads. */
        @JavascriptInterface
        public void saveFile(String name, String mime, String base64) {
            try {
                byte[] bytes = Base64.decode(base64, Base64.DEFAULT);
                if (name == null || name.isEmpty()) name = "download";
                if (mime == null || mime.isEmpty()) mime = "application/octet-stream";
                if (Build.VERSION.SDK_INT >= 29) {
                    ContentValues v = new ContentValues();
                    v.put(MediaStore.Downloads.DISPLAY_NAME, name);
                    v.put(MediaStore.Downloads.MIME_TYPE, mime);
                    Uri uri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
                    try (OutputStream o = getContentResolver().openOutputStream(uri)) { o.write(bytes); }
                } else {
                    File dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS);
                    dir.mkdirs();
                    try (FileOutputStream o = new FileOutputStream(new File(dir, name))) { o.write(bytes); }
                }
                final String n = name;
                runOnUiThread(() -> Toast.makeText(MainActivity.this, "Saved to Downloads: " + n, Toast.LENGTH_LONG).show());
            } catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this, "Could not save the file", Toast.LENGTH_LONG).show());
            }
        }

        @JavascriptInterface
        public void vibrate(int ms) {
            android.os.Vibrator vb = (android.os.Vibrator) getSystemService(VIBRATOR_SERVICE);
            if (vb != null) vb.vibrate(ms);
        }
    }

    /** Small script added to every page: share, status-bar colour from <meta name="theme-color">, file downloads. */
    static final String SHIM = "(function(){if(window.__htmlapk)return;window.__htmlapk=1;var A=window.HtmlApk;" +
            "navigator.share=function(d){d=d||{};A.share(d.title||document.title||'',d.text||'',d.url||'');return Promise.resolve()};" +
            "navigator.canShare=function(){return true};" +
            "function bar(){var m=document.querySelector('meta[name=theme-color]');var c=m&&m.content;" +
            "if(!c){var bg=getComputedStyle(document.body).backgroundColor;var r=bg&&bg.match(/\\d+/g);if(r&&r.length>=3&&!(r.length>3&&+r[3]===0))c='#'+[r[0],r[1],r[2]].map(function(x){return(+x).toString(16).padStart(2,'0')}).join('')}" +
            "if(c&&/^#[0-9a-f]{3,8}$/i.test(c.trim())){c=c.trim();if(c.length===4)c='#'+c[1]+c[1]+c[2]+c[2]+c[3]+c[3];A.setBarColor(c.slice(0,7))}}" +
            "bar();new MutationObserver(bar).observe(document.head,{subtree:true,childList:true,attributes:true});" +
            "function save(href,name){fetch(href).then(function(r){return r.blob()}).then(function(b){var f=new FileReader();f.onload=function(){A.saveFile(name||'download',b.type||'',String(f.result).split(',')[1]||'')};f.readAsDataURL(b)})}" +
            "document.addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('a[download]');if(!a)return;var h=a.href||'';" +
            "if(/^(blob|data):/.test(h)||h.indexOf('" + HOST + "')>0){e.preventDefault();save(h,a.getAttribute('download')||h.split('/').pop())}},true);" +
            "var oc=HTMLAnchorElement.prototype.click;HTMLAnchorElement.prototype.click=function(){if(this.hasAttribute('download')&&/^(blob|data):/.test(this.href)){save(this.href,this.getAttribute('download'));return}return oc.apply(this,arguments)};" +
            "navigator.vibrate=function(p){A.vibrate(Array.isArray(p)?(p[0]||0):(+p||0));return true};" +
            "})()";
}
