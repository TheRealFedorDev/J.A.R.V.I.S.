import 'package:shared_preferences/shared_preferences.dart';

class AppConfig {
  static const keyHost = 'host';
  static const keyPhoneKey = 'phone_key';
  static const defaultHost = 'http://192.168.1.100:8765';

  static Future<String> host() async {
    final p = await SharedPreferences.getInstance();
    return p.getString(keyHost) ?? defaultHost;
  }

  static Future<void> setHost(String v) async {
    final p = await SharedPreferences.getInstance();
    await p.setString(keyHost, v);
  }

  static Future<String> phoneKey() async {
    final p = await SharedPreferences.getInstance();
    return p.getString(keyPhoneKey) ?? '';
  }

  static Future<void> setPhoneKey(String v) async {
    final p = await SharedPreferences.getInstance();
    await p.setString(keyPhoneKey, v);
  }

  static Future<Map<String, String>> headers() async {
    return {'X-Phone-Key': await phoneKey()};
  }
}
