import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_background_service/flutter_background_service.dart';
import 'package:go_router/go_router.dart';

import '../api/api.dart';
import '../services/voice.dart';
import '../utils/config.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeState();
}

class _HomeState extends State<HomeScreen> {
  final api = PhoneApi();
  final voice = VoiceService();
  final _cmdCtrl = TextEditingController();

  String reply = '';
  String source = '';
  String heard = '';
  String status = '';
  bool rec = false;
  String? recPath;
  bool serviceOn = false;
  bool online = false;

  StreamSubscription<Map<String, dynamic>?>? _subReply;
  StreamSubscription<Map<String, dynamic>?>? _subChunk;
  StreamSubscription<Map<String, dynamic>?>? _subStatus;

  @override
  void initState() {
    super.initState();
    _boot();
  }

  @override
  void dispose() {
    _subReply?.cancel();
    _subChunk?.cancel();
    _subStatus?.cancel();
    _cmdCtrl.dispose();
    super.dispose();
  }

  Future<void> _boot() async {
    final last = await AppConfig.lastReply();
    final svc = FlutterBackgroundService();
    final running = await svc.isRunning();

    _subReply = svc.on('reply').listen((e) {
      if (e == null) return;
      setState(() {
        reply = (e['reply'] ?? '').toString();
        source = (e['source'] ?? '').toString();
        status = '';
        online = true;
      });
    });
    _subChunk = svc.on('llm_chunk').listen((e) {
      final c = (e?['chunk'] ?? '').toString();
      if (c.isEmpty) return;
      setState(() => reply = reply + c);
    });
    _subStatus = svc.on('status').listen((e) {
      final t = (e?['text'] ?? '').toString();
      if (t.isEmpty) return;
      setState(() => status = t);
    });

    if (mounted) {
      setState(() {
        reply = (last['reply'] ?? '').toString();
        source = (last['source'] ?? '').toString();
        serviceOn = running;
      });
    }
  }

  Future<void> _toggleService() async {
    final svc = FlutterBackgroundService();
    if (await svc.isRunning()) {
      svc.invoke('stop');
      await Future<void>.delayed(const Duration(milliseconds: 500));
    } else {
      await svc.startService();
    }
    final running = await svc.isRunning();
    if (mounted) setState(() => serviceOn = running);
  }

  Future<void> _sendCmd() async {
    final text = _cmdCtrl.text.trim();
    if (text.isEmpty) return;
    _cmdCtrl.clear();
    setState(() {
      heard = text;
      status = 'Думаю…';
    });
    try {
      final res = await api.cmd(text);
      if (!mounted) return;
      final a = (res['reply'] ?? '').toString();
      setState(() {
        status = '';
        if (a.isNotEmpty) {
          reply = a;
          source = (res['source'] ?? '').toString();
        }
      });
    } catch (_) {
      if (mounted) setState(() => status = '');
    }
  }

  Future<void> _pttStart() async {
    recPath = await voice.start();
    setState(() => rec = recPath != null);
  }

  Future<void> _pttStop() async {
    final path = recPath;
    if (path == null) return;
    setState(() {
      rec = false;
      status = 'Распознаю…';
    });
    final res = await voice.stopAndSend(path);
    recPath = null;
    if (mounted) {
      setState(() {
        status = '';
        if (res != null) {
          final t = (res['transcript'] ?? '').toString();
          final a = (res['reply'] ?? '').toString();
          if (t.isNotEmpty) heard = t;
          if (a.isNotEmpty) reply = a;
        }
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Berta'),
        actions: [
          IconButton(icon: const Icon(Icons.task_alt), onPressed: () => context.go('/tasks')),
          IconButton(icon: const Icon(Icons.settings), onPressed: () => context.go('/settings')),
        ],
      ),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Icon(serviceOn ? Icons.circle : Icons.circle_outlined,
                    size: 12, color: serviceOn ? Colors.greenAccent : Colors.grey),
                const SizedBox(width: 8),
                Text(serviceOn ? 'Мост активен (фон)' : 'Мост выключен'),
                const Spacer(),
                TextButton(onPressed: _toggleService, child: Text(serviceOn ? 'Стоп' : 'Старт')),
              ],
            ),
            const Divider(),
            const Text('Последний ответ', style: TextStyle(color: Colors.white70, fontSize: 12)),
            const SizedBox(height: 8),
            if (status.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Row(
                  children: [
                    const SizedBox(
                      width: 14, height: 14,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(status,
                          style: const TextStyle(color: Colors.white70, fontSize: 13)),
                    ),
                  ],
                ),
              ),
            Expanded(
              child: Container(
                width: double.infinity,
                padding: const EdgeInsets.all(16),
                decoration: BoxDecoration(
                  color: Colors.black26,
                  borderRadius: BorderRadius.circular(16),
                ),
                child: SingleChildScrollView(
                  child: Text(
                    reply.isEmpty ? '…пока пусто. Спросите голосом или текстом.' : reply,
                    style: const TextStyle(fontSize: 20, height: 1.35),
                  ),
                ),
              ),
            ),
            if (heard.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 10),
                child: Text('услышано: $heard',
                    style: const TextStyle(color: Colors.white54, fontSize: 13)),
              ),
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _cmdCtrl,
                    decoration: const InputDecoration(hintText: 'Текстовая команда…'),
                    onSubmitted: (_) => _sendCmd(),
                  ),
                ),
                const SizedBox(width: 8),
                IconButton.filled(onPressed: _sendCmd, icon: const Icon(Icons.send)),
              ],
            ),
            const SizedBox(height: 8),
            GestureDetector(
              onLongPressStart: (_) => _pttStart(),
              onLongPressEnd: (_) => _pttStop(),
              child: Container(
                height: 56,
                alignment: Alignment.center,
                decoration: BoxDecoration(
                  color: rec ? Colors.red : Colors.blueGrey,
                  borderRadius: BorderRadius.circular(14),
                ),
                child: Text(rec ? 'Отпустите — отправить' : 'Удерживайте — говорить',
                    style: const TextStyle(fontWeight: FontWeight.bold)),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
