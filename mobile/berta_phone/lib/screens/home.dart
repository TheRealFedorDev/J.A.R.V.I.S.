import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import '../api/api.dart';
import '../services/voice.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeState();
}

class _HomeState extends State<HomeScreen> {
  final api = PhoneApi();
  final voice = VoiceService();
  String last = '';
  bool rec = false;
  String? recPath;

  Future<void> _pttStart() async {
    recPath = await voice.start();
    setState(() => rec = recPath != null);
  }

  Future<void> _pttStop() async {
    if (recPath == null) return;
    final res = await voice.stopAndSend(recPath!);
    setState(() => rec = false);
    if (res != null && res['answer'] != null && res['answer'].toString().isNotEmpty) {
      last = res['answer'].toString();
      setState(() {});
    } else if (res != null && res['transcript'] != null) {
      last = 'heard: ${res['transcript']}';
      setState(() {});
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Berta Phone'), actions: [
        IconButton(icon: const Icon(Icons.task_alt), onPressed: () => context.go('/tasks')),
        IconButton(icon: const Icon(Icons.settings), onPressed: () => context.go('/settings')),
      ]),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Text(last.isEmpty ? 'Hold mic to speak' : last, textAlign: TextAlign.center),
            const SizedBox(height: 24),
            GestureDetector(
              onLongPressStart: (_) => _pttStart(),
              onLongPressEnd: (_) => _pttStop(),
              child: Container(
                width: 160, height: 160,
                decoration: BoxDecoration(shape: BoxShape.circle, color: rec ? Colors.red : Colors.blueGrey),
                child: const Icon(Icons.mic, size: 64, color: Colors.white),
              ),
            ),
            const SizedBox(height: 12),
            const Text('Push-to-talk'),
          ],
        ),
      ),
    );
  }
}
