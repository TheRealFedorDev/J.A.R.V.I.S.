import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';
import 'package:record/record.dart';

import '../utils/config.dart';

class VoiceService {
  final _rec = AudioRecorder();

  Future<bool> hasPerm() => _rec.hasPermission();

  Future<String?> start() async {
    if (!await hasPerm()) return null;
    final dir = await getTemporaryDirectory();
    final path = '${dir.path}/berta_${DateTime.now().millisecondsSinceEpoch}.wav';
    await _rec.start(
      const RecordConfig(encoder: AudioEncoder.wav, sampleRate: 16000, numChannels: 1),
      path: path,
    );
    return path;
  }

  Future<Map<String, dynamic>?> stopAndSend(String path) async {
    await _rec.stop();
    final f = File(path);
    if (!await f.exists()) return null;

    final host = await AppConfig.host();
    final headers = await AppConfig.headers();
    final req = http.MultipartRequest('POST', Uri.parse('$host/api/phone/voice'));
    req.headers.addAll(headers);
    req.files.add(await http.MultipartFile.fromPath('file', f.path, filename: 'voice.wav'));

    try {
      final streamed = await req.send().timeout(const Duration(seconds: 90));
      final body = await streamed.stream.bytesToString();
      if (streamed.statusCode == 200) {
        try {
          return jsonDecode(body) as Map<String, dynamic>;
        } catch (_) {
          return <String, dynamic>{};
        }
      }
    } catch (_) {
      // сеть/сервер недоступны
    } finally {
      try {
        await f.delete();
      } catch (_) {}
    }
    return null;
  }
}
