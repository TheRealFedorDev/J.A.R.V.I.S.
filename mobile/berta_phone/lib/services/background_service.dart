import 'dart:async';
import 'dart:convert';
import 'dart:ui';

import 'package:flutter_background_service/flutter_background_service.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import '../utils/config.dart';

/// Канал уведомления создаёт сам плагин (FOREGROUND_DEFAULT).
/// Свой channelId передавать нельзя: плагин создаёт канал только когда id == null,
/// иначе startForeground падает с "invalid channel" и Android убивает приложение.
const kNotifId = 878;

/// Мост телефон <-> ПК, живущий в фоне.
///
/// Держит WebSocket к серверу Berta и на каждое событие `router` (ответ
/// нейросети/правил) обновляет уведомление и пересылает текст в UI.
class BertaBridge {
  static Future<void> init() async {
    final service = FlutterBackgroundService();
    await service.configure(
      androidConfiguration: AndroidConfiguration(
        onStart: onStart,
        autoStart: true,
        isForegroundMode: true,
        autoStartOnBoot: false,
        initialNotificationTitle: 'Berta',
        initialNotificationContent: 'Подключаюсь…',
        foregroundServiceNotificationId: kNotifId,
        foregroundServiceTypes: [AndroidForegroundType.dataSync],
      ),
      iosConfiguration: IosConfiguration(autoStart: false),
    );
  }
}

void _notify(ServiceInstance service, String content) {
  if (service is AndroidServiceInstance) {
    service.setForegroundNotificationInfo(
      title: 'Berta',
      content: content.length > 180 ? '${content.substring(0, 180)}…' : content,
    );
  }
}

@pragma('vm:entry-point')
void onStart(ServiceInstance service) async {
  DartPluginRegistrant.ensureInitialized();

  if (service is AndroidServiceInstance) {
    service.on('setAsForeground').listen((_) => service.setAsForegroundService());
    service.on('setAsBackground').listen((_) => service.setAsBackgroundService());
  }
  service.on('stop').listen((_) => service.stopSelf());

  _notify(service, 'Подключаюсь…');

  while (true) {
    try {
      await _connect(service);
    } catch (_) {
      // сеть недоступна/сервер лёг — просто повторим
    }
    _notify(service, 'Переподключение…');
    await Future<void>.delayed(const Duration(seconds: 5));
  }
}

Future<void> _connect(ServiceInstance service) async {
  final host = await AppConfig.host();
  if (host.isEmpty || host.contains('192.168.1.100')) {
    _notify(service, 'Задайте адрес сервера в настройках');
    await Future<void>.delayed(const Duration(seconds: 5));
    return;
  }

  final uri = await AppConfig.wsUri();
  final channel = WebSocketChannel.connect(uri);
  await channel.ready.timeout(const Duration(seconds: 12));
  _notify(service, 'Онлайн · ${uri.host}');

  await for (final data in channel.stream) {
    await _handle(service, data);
  }
}

Future<void> _handle(ServiceInstance service, dynamic data) async {
  if (data is! String) return;
  Map<String, dynamic> msg;
  try {
    msg = jsonDecode(data) as Map<String, dynamic>;
  } catch (_) {
    return;
  }

  final type = msg['type'];
  final d = msg['data'];

  if (type == 'llm_chunk' && d is Map) {
    service.invoke('llm_chunk', {'chunk': (d['chunk'] ?? '').toString()});
    return;
  }

  // прогресс («Думаю…», «Ищу в интернете…») — в UI и в уведомление,
  // чтобы было видно, что запрос не потерялся
  if (type == 'status' && d is Map) {
    final text = (d['text'] ?? '').toString();
    if (text.isEmpty) return;
    service.invoke('status', {
      'text': text,
      'kind': (d['kind'] ?? '').toString(),
    });
    _notify(service, text);
    return;
  }

  if (type == 'router' && d is Map) {
    final reply = (d['reply'] ?? '').toString();
    if (reply.isEmpty) return;
    final text = (d['text'] ?? '').toString();
    final source = (d['source'] ?? '').toString();
    await AppConfig.setLastReply(reply, source);
    service.invoke('reply', {'reply': reply, 'text': text, 'source': source});
    _notify(service, reply);
  }
}
