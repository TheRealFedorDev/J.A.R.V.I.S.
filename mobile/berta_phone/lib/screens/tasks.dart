import 'package:flutter/material.dart';
import '../api/api.dart';

class TasksScreen extends StatefulWidget {
  const TasksScreen({super.key});

  @override
  State<TasksScreen> createState() => _TasksState();
}

class _TasksState extends State<TasksScreen> {
  final api = PhoneApi();
  List<dynamic> tasks = [];
  final ctrl = TextEditingController();

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    tasks = await api.tasks();
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Tasks')),
      body: Column(
        children: [
          Row(children: [
            Expanded(child: TextField(controller: ctrl, decoration: const InputDecoration(hintText: 'New task'))),
            IconButton(icon: const Icon(Icons.add), onPressed: () async { if (ctrl.text.isNotEmpty) { await api.addTask(ctrl.text.trim()); ctrl.clear(); await _load(); } }),
          ]),
          Expanded(child: ListView.builder(itemCount: tasks.length, itemBuilder: (_, i) {
            final t = tasks[i] as Map<String, dynamic>;
            return ListTile(title: Text(t['text']?.toString() ?? ''), trailing: IconButton(icon: const Icon(Icons.delete), onPressed: () async { await api.deleteTask(t['id'] as int); await _load(); }));
          })),
        ],
      ),
    );
  }
}
