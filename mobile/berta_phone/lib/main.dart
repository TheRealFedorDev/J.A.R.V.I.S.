import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'screens/home.dart';
import 'screens/tasks.dart';
import 'screens/settings.dart';

void main() {
  runApp(const ProviderScope(child: BertaApp()));
}

final _router = GoRouter(
  routes: [
    GoRoute(path: '/', builder: (context, state) => const HomeScreen()),
    GoRoute(path: '/tasks', builder: (context, state) => const TasksScreen()),
    GoRoute(path: '/settings', builder: (context, state) => const SettingsScreen()),
  ],
);

class BertaApp extends StatelessWidget {
  const BertaApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp.router(
      title: 'Berta Phone',
      theme: ThemeData.dark(useMaterial3: true),
      routerConfig: _router,
    );
  }
}
