"""Патч сгенерированного android/ проекта под фоновый BLE/WS-мост.

Вызывается в CI после `flutter create --platforms=android`. Добавляет:
  * разрешения (INTERNET, FOREGROUND_SERVICE_DATA_SYNC, POST_NOTIFICATIONS, ...);
  * объявление сервиса flutter_background_service с foregroundServiceType;
  * minSdk 23 (нужен плагинам record/permission_handler).

Иконку уведомления НЕ создаём: плагин flutter_background_service уже поставляет
корректные PNG (ic_bg_service_small) в drawable-*dpi. Своя векторная иконка
перекрывала их и приводила к CannotPostForegroundServiceNotificationException.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
ANDROID = ROOT / "android"
MANIFEST = ANDROID / "app" / "src" / "main" / "AndroidManifest.xml"

PERMISSIONS = [
    "android.permission.INTERNET",
    "android.permission.ACCESS_NETWORK_STATE",
    "android.permission.FOREGROUND_SERVICE",
    "android.permission.FOREGROUND_SERVICE_DATA_SYNC",
    "android.permission.POST_NOTIFICATIONS",
    "android.permission.WAKE_LOCK",
    "android.permission.RECORD_AUDIO",
]

SERVICE_DECL = (
    '        <service\n'
    '            android:name="id.flutter.flutter_background_service.BackgroundService"\n'
    '            android:foregroundServiceType="dataSync"\n'
    '            android:exported="false"\n'
    '            tools:replace="android:exported" />\n'
)

def patch_manifest() -> None:
    txt = MANIFEST.read_text(encoding="utf-8")

    if "xmlns:tools" not in txt:
        txt = txt.replace("<manifest", '<manifest xmlns:tools="http://schemas.android.com/tools"', 1)

    missing = [p for p in PERMISSIONS if f'android:name="{p}"' not in txt]
    if missing:
        lines = "".join(f'    <uses-permission android:name="{p}" />\n' for p in missing)
        m = re.search(r"<manifest[^>]*>", txt)
        txt = txt[: m.end()] + "\n" + lines + txt[m.end():]

    if "flutter_background_service.BackgroundService" not in txt:
        txt = txt.replace("</application>", SERVICE_DECL + "    </application>")

    MANIFEST.write_text(txt, encoding="utf-8")
    print(f"[patch] manifest updated ({len(missing)} perms added)")


def patch_min_sdk() -> None:
    for name in ("build.gradle.kts", "build.gradle"):
        path = ANDROID / "app" / name
        if not path.exists():
            continue
        txt = path.read_text(encoding="utf-8")
        txt = re.sub(r"minSdk\s*=\s*flutter\.minSdkVersion", "minSdk = 23", txt)
        txt = re.sub(r"minSdkVersion\s+flutter\.minSdkVersion", "minSdkVersion 23", txt)
        path.write_text(txt, encoding="utf-8")
        print(f"[patch] minSdk set in {name}")


def main() -> int:
    if not MANIFEST.exists():
        print(f"manifest not found: {MANIFEST}", file=sys.stderr)
        return 1
    patch_manifest()
    patch_min_sdk()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
