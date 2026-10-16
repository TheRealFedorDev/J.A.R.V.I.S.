import 'dart:io';
import 'package:record/record.dart';
import 'package:path_provider/path_provider.dart';
import 'package:http/http.dart' as http;
import '../utils/config.dart';

class VoiceService {
  final _rec = AudioRecorder();

  Future<bool> hasPerm() => _rec.hasPermission();

  Future<String?> start() async {
    if (!await hasPerm()) return null;
    final dir = await getTemporaryDirectory();
    final path = '${dir.path}/berta_${DateTime.now().millisecondsSinceEpoch}.wav';
    await _rec.start(const RecordConfig(encoder: AudioEncoder.wav, sampleRate: 16000, numChannels: 1), path: path);
    return path;
  }

  Future<Map<String, dynamic>?> stopAndSend(String path) async {
    await _rec.stop();
    final f = File(path);
    if (!await f.exists()) return null;
    final host = await AppConfig.host();
    final h = await AppConfig.headers();
    final req = http.MultipartRequest('POST', Uri.parse('$host/api/phone/voice'));
    req.headers.addAll(h);
    req.files.add(await http.MultipartFile.fromPath('file', f.path, filename: 'voice.wav'));
    final resp = await req.send();
    final body = await resp.stream.bytesToString();
    if (resp.statusCode == 200) {
      try { return Map<String, dynamic>.from(Uri.splitQueryString(body)); } catch (_) {}
      try { return (await http.Response.fromStream(resp)).body.isEmpty ? {} : Map<String, dynamic>.from(Uri.splitQueryString(body)); } catch (_) {}
    }
    try { await f.delete(); } catch (_) {}
    return null;
  }
}
