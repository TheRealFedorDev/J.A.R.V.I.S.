import 'package:shared_preferences/shared_preferences.dart';

class AppConfig {
  static const keyHost = 'host';
  static const keyPhoneKey = 'phone_key';
  static const keyLastReply = 'last_reply';
  static const keyLastSource = 'last_reply_source';
  static const keyLastTs = 'last_reply_ts';
  static const defaultHost = 'http://192.168.1.100:8765';

  /// Приводим адрес к виду без хвостового слэша и со схемой.
  static String normalizeHost(String v) {
    var s = v.trim();
    if (s.isEmpty) return s;
    if (!s.startsWith('http://') && !s.startsWith('https://')) {
      s = 'http://$s';
    }
    return s.replaceAll(RegExp(r'/+$'), '');
  }

  static Future<String> host() async {
    final p = await SharedPreferences.getInstance();
    return p.getString(keyHost) ?? defaultHost;
  }

  static Future<void> setHost(String v) async {
    final p = await SharedPreferences.getInstance();
    await p.setString(keyHost, normalizeHost(v));
  }

  static Future<String> phoneKey() async {
    final p = await SharedPreferences.getInstance();
    return p.getString(keyPhoneKey) ?? '';
  }

  static Future<void> setPhoneKey(String v) async {
    final p = await SharedPreferences.getInstance();
    await p.setString(keyPhoneKey, v.trim());
  }

  static Future<Map<String, String>> headers() async {
    return {'X-Phone-Key': await phoneKey()};
  }

  /// ws:// или wss:// адрес WebSocket-канала к серверу Berta.
  static Future<Uri> wsUri() async {
    final h = normalizeHost(await host());
    final scheme = h.startsWith('https://') ? 'wss' : 'ws';
    final rest = h.replaceFirst(RegExp(r'^https?://'), '');
    return Uri.parse('$scheme://$rest/ws');
  }

  static Future<void> setLastReply(String reply, String source) async {
    final p = await SharedPreferences.getInstance();
    await p.setString(keyLastReply, reply);
    await p.setString(keyLastSource, source);
    await p.setInt(keyLastTs, DateTime.now().millisecondsSinceEpoch);
  }

  static Future<Map<String, dynamic>> lastReply() async {
    final p = await SharedPreferences.getInstance();
    await p.reload();
    return {
      'reply': p.getString(keyLastReply) ?? '',
      'source': p.getString(keyLastSource) ?? '',
      'ts': p.getInt(keyLastTs) ?? 0,
    };
  }
}
