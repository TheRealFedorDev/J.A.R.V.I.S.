import 'package:flutter/material.dart';
import '../utils/config.dart';
import '../api/api.dart';

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

  Future<void> _load() async {
    hostCtrl.text = await AppConfig.host();
    keyCtrl.text = await AppConfig.phoneKey();
    ok = await PhoneApi().health();
    if (mounted) setState(() {});
  }

  Future<void> _save() async {
    await AppConfig.setHost(hostCtrl.text.trim());
    await AppConfig.setPhoneKey(keyCtrl.text.trim());
    ok = await PhoneApi().health();
    if (mounted) setState(() {});
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(ok ? 'Connected' : 'Failed')));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Settings')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          TextField(controller: hostCtrl, decoration: const InputDecoration(labelText: 'Host (http://192.168.XXX.XXX:8765)')),
          TextField(controller: keyCtrl, decoration: const InputDecoration(labelText: 'Phone API Key')),
          const SizedBox(height: 12),
          FilledButton(onPressed: _save, child: const Text('Save & Test')),
          const SizedBox(height: 12),
          Text('Status: ${ok ? 'OK' : 'DISCONNECTED'}'),
        ],
      ),
    );
  }
}
