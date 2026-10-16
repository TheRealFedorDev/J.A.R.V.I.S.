import 'dart:convert';
import 'package:web_socket_channel/web_socket_channel.dart';
import '../utils/config.dart';

typedef WsEvent = void Function(Map<String, dynamic> e);

class PhoneWs {
  WebSocketChannel? _ch;
  bool connected = false;

  Future<void> connect({required WsEvent onEvent}) async {
    final host = await AppConfig.host();
    final key = await AppConfig.phoneKey();
    // try ws scheme
    var uriStr = host.replaceFirst('http://', 'ws://').replaceFirst('https://', 'wss://');
    final uri = Uri.parse('$uriStr/ws?token=$key');
    _ch = WebSocketChannel.connect(uri);
    connected = true;
    _ch!.stream.listen((d) {
      try {
        if (d is String) {
          final m = jsonDecode(d);
          if (m is Map<String, dynamic>) onEvent(m);
        }
      } catch (_) {}
    }, onDone: () => connected = false, onError: (_) => connected = false);
  }

  void close() {
    _ch?.sink.close();
    connected = false;
  }
}
