import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:permission_handler/permission_handler.dart';

import 'screens/home.dart';
import 'screens/settings.dart';
import 'screens/tasks.dart';
import 'services/background_service.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Уведомление foreground-сервиса на Android 13+ требует разрешения.
  if (Platform.isAndroid) {
    try {
      await Permission.notification.request();
    } catch (_) {}
  }

  // Мост телефон <-> ПК стартует сразу и живёт в фоне.
  await BertaBridge.init();

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
