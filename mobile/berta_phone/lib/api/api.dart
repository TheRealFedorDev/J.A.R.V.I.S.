import 'dart:convert';
import 'package:http/http.dart' as http;
import '../utils/config.dart';

class PhoneApi {
  Future<bool> health() async {
    final host = await AppConfig.host();
    final h = await AppConfig.headers();
    try {
      final r = await http.get(Uri.parse('$host/api/phone/health'), headers: h).timeout(const Duration(seconds: 3));
      return r.statusCode == 200;
    } catch (_) {
      return false;
    }
  }

  Future<List<dynamic>> tasks() async {
    final host = await AppConfig.host();
    final h = await AppConfig.headers();
    final r = await http.get(Uri.parse('$host/api/phone/tasks'), headers: h);
    if (r.statusCode == 200) return (jsonDecode(r.body)['tasks'] as List);
    return [];
  }

  Future<Map<String, dynamic>?> addTask(String text) async {
    final host = await AppConfig.host();
    final h = await AppConfig.headers();
    final r = await http.post(Uri.parse('$host/api/phone/tasks'), headers: {...h, 'Content-Type': 'application/json'}, body: jsonEncode({'text': text}));
    if (r.statusCode == 200) return jsonDecode(r.body) as Map<String, dynamic>;
    return null;
  }

  Future<bool> deleteTask(int id) async {
    final host = await AppConfig.host();
    final h = await AppConfig.headers();
    final r = await http.delete(Uri.parse('$host/api/phone/tasks/$id'), headers: h);
    return r.statusCode == 200;
  }

  Future<bool> patchTask(int id, String text) async {
    final host = await AppConfig.host();
    final h = await AppConfig.headers();
    final r = await http.patch(Uri.parse('$host/api/phone/tasks/$id'), headers: {...h, 'Content-Type': 'application/json'}, body: jsonEncode({'text': text}));
    return r.statusCode == 200;
  }

  Future<Map<String, dynamic>> cmd(String text) async {
    final host = await AppConfig.host();
    final h = await AppConfig.headers();
    final r = await http.post(Uri.parse('$host/api/phone/cmd'), headers: {...h, 'Content-Type': 'application/json'}, body: jsonEncode({'text': text}));
    if (r.statusCode == 200) return jsonDecode(r.body);
    return {'matched': false, 'answer': ''};
  }

  Future<Map<String, dynamic>> mic(bool enabled) async {
    final host = await AppConfig.host();
    final h = await AppConfig.headers();
    final r = await http.post(Uri.parse('$host/api/phone/mic'), headers: {...h, 'Content-Type': 'application/json'}, body: jsonEncode({'enabled': enabled}));
    if (r.statusCode == 200) return jsonDecode(r.body);
    return {'ok': false};
  }

  Future<Map<String, dynamic>> tts(bool enabled) async {
    final host = await AppConfig.host();
    final h = await AppConfig.headers();
    final r = await http.post(Uri.parse('$host/api/phone/tts'), headers: {...h, 'Content-Type': 'application/json'}, body: jsonEncode({'enabled': enabled}));
    if (r.statusCode == 200) return jsonDecode(r.body);
    return {'ok': false};
  }
}
