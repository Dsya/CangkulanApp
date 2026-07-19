package com.rj.cangkulan;

import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import androidx.appcompat.app.AppCompatActivity;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

public class MainActivity extends AppCompatActivity {

    private static final String SERVER_URL = "http://127.0.0.1:8000";
    private WebView webView;
    private boolean serverStarted = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        webView = new WebView(this);
        setContentView(webView);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        webView.setWebViewClient(new WebViewClient());

        startPythonServer();
        loadWhenReady();
    }

    private void startPythonServer() {
        if (serverStarted) return;
        serverStarted = true;

        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(this));
        }

        new Thread(() -> {
            Python py = Python.getInstance();
            py.getModule("server_cangkulan").callAttr("run_server");
        }, "python-cangkulan-server").start();
    }

    private void loadWhenReady() {
        new Handler(Looper.getMainLooper()).postDelayed(
                () -> webView.loadUrl(SERVER_URL), 1500);
    }

    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
