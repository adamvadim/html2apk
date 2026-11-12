# html2apk

Turn an HTML app (for example one made with Claude) into an installable Android APK in seconds, without Android Studio.

- `shell/` – a generic Android app that shows `assets/www/index.html` full screen. GitHub Actions builds it once and publishes it as the release **shell-latest**.
- `html2apk.py` – swaps your HTML, app name, icon and version into that shell and signs it.

```
python3 html2apk.py page.html --name "My App"
```

The same app name always gives the same app ID and signing key, so a newer APK installs over the older one as an update.
