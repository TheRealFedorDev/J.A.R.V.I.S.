# Berta Phone (Android) MVP

## Goal
Local-only Android app to control J.A.R.V.I.S from phone: commands, tasks, push-to-talk voice, mic/tts toggle, live state via WebSocket.

## Stack
- Flutter 3.x
- Packages: http, web_socket_channel, record, permission_handler, shared_preferences, url_launcher (optional)

## Setup
```bash
cd mobile/berta_phone
flutter pub get
flutter run
```

## Build APK
```bash
flutter build apk --release --split-per-abi
# or single
flutter build apk --release
```

Output: `build/app/outputs/flutter-apk/`

## Connect to PC
1. Get PC IP on Wi-Fi (ipconfig)
2. Generate PHONE_API_KEY on PC:
   ```powershell
   .venv\Scripts\python.exe -c "import secrets; print('berta-phone-'+secrets.token_urlsafe(24))"
   ```
3. Add to PC `.env`: `PHONE_API_KEY=...`, `PHONE_BIND=0.0.0.0`
4. Restart PC server (`.venv\Scripts\python.exe main.py` or voice/web)
5. Set in app: Host `http://192.168.XXX.XXX:8765`, Key from step 2

## Permissions (Android)
- RECORD_AUDIO
- INTERNET
- ACCESS_NETWORK_STATE
- POST_NOTIFICATIONS (optional)

## Notes
- Only local network. No cloud.
- Push-to-talk sends 16kHz mono WAV to `/api/phone/voice` (MVP)
- WebSocket at `/ws?token=KEY` or use header via channel? start simple: connect with key in query or pass via header if supported later
