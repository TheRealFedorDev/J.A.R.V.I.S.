import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_background_service/flutter_background_service.dart';
import 'package:permission_handler/permission_handler.dart';

import '../api/api.dart';
import '../utils/config.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});

  @override
  State<SettingsScreen> createState() => _SettingsState();
}

class _SettingsState extends State<SettingsScreen> {
  final hostCtrl = TextEditingController();
  final keyCtrl = TextEditingController();
  bool ok = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    hostCtrl.dispose();
    keyCtrl.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    hostCtrl.text = await AppConfig.host();
    keyCtrl.text = await AppConfig.phoneKey();
    ok = await PhoneApi().health();
    if (mounted) setState(() {});
  }

  Future<void> _save() async {
    await AppConfig.setHost(hostCtrl.text);
    await AppConfig.setPhoneKey(keyCtrl.text);
    hostCtrl.text = await AppConfig.host();

    if (Platform.isAndroid) {
      try {
        await Permission.notification.request();
      } catch (_) {}
    }

    // Перезапуск фонового моста, чтобы он перечитал адрес/ключ.
    final svc = FlutterBackgroundService();
    if (await svc.isRunning()) {
      svc.invoke('stop');
      await Future<void>.delayed(const Duration(milliseconds: 600));
    }
    await svc.startService();

    ok = await PhoneApi().health();
    if (mounted) {
      setState(() {});
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(ok ? 'Сохранено, связь есть' : 'Сохранено, связи нет')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Настройки')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          const Text('Адрес ПК (можно https через Tailscale)',
              style: TextStyle(color: Colors.white70)),
          TextField(
            controller: hostCtrl,
            decoration: const InputDecoration(hintText: 'http://192.168.1.100:8765'),
          ),
          const SizedBox(height: 16),
          const Text('Ключ доступа (PHONE_API_KEY)', style: TextStyle(color: Colors.white70)),
          TextField(controller: keyCtrl, decoration: const InputDecoration(hintText: 'berta-...')),
          const SizedBox(height: 16),
          FilledButton(onPressed: _save, child: const Text('Сохранить и проверить')),
          const SizedBox(height: 12),
          Text('Связь: ${ok ? 'OK' : 'нет'}'),
          const SizedBox(height: 24),
          const Text(
            'Мост работает в фоне: пока он запущен, ответы Berta приходят в '
            'уведомление и на этот экран, даже когда приложение свёрнуто.',
            style: TextStyle(color: Colors.white54, fontSize: 13),
          ),
        ],
      ),
    );
  }
}
