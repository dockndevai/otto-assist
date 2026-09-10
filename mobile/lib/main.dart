// Orrin mobile — a voice remote to your Orrin server running on your Mac.
//
// The phone captures speech (on-device STT), sends the transcript over the SAME WebSocket
// protocol the desktop dashboard uses ({"type":"user_text", ...}), and speaks the reply back
// with on-device TTS. All the real work — Claude Code, your files, shell, MCP, controlling the
// laptop — happens on the Mac. The phone is just the mic, speaker, and screen.
import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;
import 'package:flutter_tts/flutter_tts.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import 'package:web_socket_channel/status.dart' as ws_status;

void main() => runApp(const OrrinApp());

class OrrinApp extends StatelessWidget {
  const OrrinApp({super.key});
  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Orrin',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF4C8DFF),
          brightness: Brightness.dark,
        ),
        scaffoldBackgroundColor: const Color(0xFF0E1116),
      ),
      home: const HomePage(),
    );
  }
}

/// One line in the conversation view.
class Line {
  Line(this.role, this.text, {this.tool = false});
  final String role; // 'you' | 'otto' | 'tool' | 'sys'
  String text;
  final bool tool;
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});
  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  // ---- connection settings (persisted) ----
  String _host = ''; // e.g. 100.x.y.z:8000  or  192.168.1.20:8000
  String _token = '';
  bool _tls = false; // ws:// over Tailscale/LAN is fine; wss:// for public tunnels

  // ---- runtime ----
  WebSocketChannel? _ch;
  StreamSubscription? _sub;
  bool _connected = false;
  bool _connecting = false;
  Timer? _reconnect;

  final _speech = stt.SpeechToText();
  bool _sttReady = false;
  bool _listening = false;
  bool _handsFree = false;

  final _tts = FlutterTts();
  bool _speaking = false;

  final List<Line> _lines = [];
  final _scroll = ScrollController();
  String _status = 'Not connected';
  String _partial = '';

  @override
  void initState() {
    super.initState();
    _load();
    _initSpeech();
    _initTts();
  }

  @override
  void dispose() {
    _reconnect?.cancel();
    _sub?.cancel();
    _ch?.sink.close();
    _tts.stop();
    super.dispose();
  }

  Future<void> _load() async {
    final p = await SharedPreferences.getInstance();
    setState(() {
      _host = p.getString('host') ?? '';
      _token = p.getString('token') ?? '';
      _tls = p.getBool('tls') ?? false;
    });
    if (_host.isNotEmpty && _token.isNotEmpty) _connect();
  }

  Future<void> _save() async {
    final p = await SharedPreferences.getInstance();
    await p.setString('host', _host);
    await p.setString('token', _token);
    await p.setBool('tls', _tls);
  }

  Future<void> _initSpeech() async {
    await Permission.microphone.request();
    _sttReady = await _speech.initialize(
      onStatus: (s) {
        if (s == 'done' || s == 'notListening') {
          if (mounted) setState(() => _listening = false);
        }
      },
      onError: (e) {
        if (mounted) setState(() => _listening = false);
      },
    );
    if (mounted) setState(() {});
  }

  Future<void> _initTts() async {
    await _tts.setSpeechRate(0.5);
    await _tts.setPitch(1.0);
    _tts.setStartHandler(() {
      if (mounted) setState(() => _speaking = true);
    });
    _tts.setCompletionHandler(() {
      if (mounted) setState(() => _speaking = false);
      // Hands-free: after Otto finishes speaking, listen again for the next command.
      if (_handsFree && _connected && !_listening) _startListening();
    });
  }

  // ---------------- WebSocket ----------------
  String get _wsUrl {
    final scheme = _tls ? 'wss' : 'ws';
    return '$scheme://$_host/ws?token=${Uri.encodeComponent(_token)}';
  }

  void _connect() {
    if (_connecting || _host.isEmpty) return;
    setState(() {
      _connecting = true;
      _status = 'Connecting…';
    });
    try {
      final ch = WebSocketChannel.connect(Uri.parse(_wsUrl));
      _ch = ch;
      _sub = ch.stream.listen(
        _onEvent,
        onDone: _onClosed,
        onError: (_) => _onClosed(),
        cancelOnError: true,
      );
      setState(() {
        _connected = true;
        _connecting = false;
        _status = 'Connected';
      });
    } catch (_) {
      _onClosed();
    }
  }

  void _onClosed() {
    if (!mounted) return;
    setState(() {
      _connected = false;
      _connecting = false;
      _status = 'Disconnected — retrying…';
    });
    _reconnect?.cancel();
    _reconnect = Timer(const Duration(seconds: 3), () {
      if (_host.isNotEmpty && _token.isNotEmpty) _connect();
    });
  }

  void _onEvent(dynamic raw) {
    Map<String, dynamic> ev;
    try {
      ev = jsonDecode(raw as String) as Map<String, dynamic>;
    } catch (_) {
      return;
    }
    final t = ev['type'] as String? ?? '';
    switch (t) {
      case 'transcript_final':
        final role = ev['role'] == 'assistant' ? 'otto' : 'you';
        final text = (ev['text'] as String? ?? '').trim();
        if (text.isEmpty) return;
        _push(role, text);
        if (role == 'otto') _speak(text);
        break;
      case 'assistant_delta':
        // Stream Otto's words into the last 'otto' line as they arrive.
        final chunk = ev['text'] as String? ?? '';
        if (chunk.isEmpty) return;
        setState(() {
          if (_lines.isNotEmpty && _lines.last.role == 'otto' && !_lines.last.tool) {
            _lines.last.text += chunk;
          } else {
            _lines.add(Line('otto', chunk));
          }
        });
        _autoScroll();
        break;
      case 'tool_use':
        final name = ev['name'] as String? ?? '';
        final detail = (ev['detail'] as String? ?? '').trim();
        _push('tool', detail.isEmpty ? name : '$name · $detail', tool: true);
        break;
      case 'status':
        setState(() => _status = (ev['text'] as String?)?.trim().isNotEmpty == true
            ? ev['text'] as String
            : 'Connected');
        break;
      case 'ask_user':
        final q = (ev['question'] as String? ?? '').trim();
        if (q.isEmpty) return;
        _push('otto', q);
        _speak(q);
        // Otto is waiting for an answer — open the mic.
        Future.delayed(const Duration(milliseconds: 300), _startListening);
        break;
      case 'notify':
        final title = ev['title'] as String? ?? '';
        final body = ev['body'] as String? ?? '';
        _push('sys', [title, body].where((s) => s.isNotEmpty).join(': '));
        break;
    }
  }

  void _push(String role, String text, {bool tool = false}) {
    setState(() => _lines.add(Line(role, text, tool: tool)));
    _autoScroll();
  }

  void _autoScroll() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.animateTo(_scroll.position.maxScrollExtent,
            duration: const Duration(milliseconds: 200), curve: Curves.easeOut);
      }
    });
  }

  void _send(String text) {
    final t = text.trim();
    if (t.isEmpty || _ch == null) return;
    _push('you', t);
    _ch!.sink.add(jsonEncode({'type': 'user_text', 'text': t}));
  }

  // ---------------- speech ----------------
  Future<void> _startListening() async {
    if (!_sttReady || _listening || !_connected) return;
    if (_speaking) await _tts.stop();
    setState(() {
      _listening = true;
      _partial = '';
    });
    await _speech.listen(
      onResult: (r) {
        setState(() => _partial = r.recognizedWords);
        if (r.finalResult) {
          final words = r.recognizedWords.trim();
          setState(() {
            _listening = false;
            _partial = '';
          });
          if (words.isNotEmpty) _send(words);
        }
      },
      listenOptions: stt.SpeechListenOptions(
        partialResults: true,
        listenFor: const Duration(seconds: 30),
        pauseFor: const Duration(seconds: 3),
      ),
    );
  }

  Future<void> _stopListening() async {
    await _speech.stop();
    setState(() => _listening = false);
  }

  Future<void> _speak(String text) async {
    if (text.isEmpty) return;
    await _tts.stop();
    await _tts.speak(text);
  }

  // ---------------- UI ----------------
  @override
  Widget build(BuildContext context) {
    final needsSetup = _host.isEmpty || _token.isEmpty;
    return Scaffold(
      appBar: AppBar(
        title: const Text('Orrin'),
        backgroundColor: Colors.transparent,
        actions: [
          IconButton(
            tooltip: 'Connection settings',
            icon: const Icon(Icons.settings),
            onPressed: _openSettings,
          ),
        ],
      ),
      body: Column(
        children: [
          _statusBar(),
          if (needsSetup)
            Expanded(child: _setupHint())
          else
            Expanded(child: _conversation()),
          if (_partial.isNotEmpty)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
              child: Align(
                alignment: Alignment.centerRight,
                child: Text('“$_partial”',
                    style: TextStyle(color: Colors.blue.shade200, fontStyle: FontStyle.italic)),
              ),
            ),
          _controls(needsSetup),
        ],
      ),
    );
  }

  Widget _statusBar() {
    final ok = _connected;
    return Container(
      width: double.infinity,
      color: (ok ? Colors.green : Colors.orange).withValues(alpha: 0.15),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      child: Row(children: [
        Icon(ok ? Icons.cloud_done : Icons.cloud_off,
            size: 16, color: ok ? Colors.green : Colors.orange),
        const SizedBox(width: 8),
        Expanded(child: Text(_status, style: const TextStyle(fontSize: 13))),
        if (_host.isNotEmpty)
          Text(_host, style: TextStyle(fontSize: 11, color: Colors.grey.shade500)),
      ]),
    );
  }

  Widget _setupHint() {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          const Icon(Icons.smartphone, size: 48, color: Colors.blueAccent),
          const SizedBox(height: 16),
          const Text('Connect to your Mac',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.w600)),
          const SizedBox(height: 8),
          Text(
            'On your Mac, open Orrin ▸ Pair phone to see the address and token, '
            'then tap the gear above to enter them.',
            textAlign: TextAlign.center,
            style: TextStyle(color: Colors.grey.shade400),
          ),
          const SizedBox(height: 20),
          FilledButton.icon(
            onPressed: _openSettings,
            icon: const Icon(Icons.settings),
            label: const Text('Enter connection details'),
          ),
        ]),
      ),
    );
  }

  Widget _conversation() {
    return ListView.builder(
      controller: _scroll,
      padding: const EdgeInsets.all(12),
      itemCount: _lines.length,
      itemBuilder: (_, i) {
        final l = _lines[i];
        if (l.tool) {
          return Padding(
            padding: const EdgeInsets.symmetric(vertical: 3),
            child: Row(children: [
              const Icon(Icons.build, size: 13, color: Colors.grey),
              const SizedBox(width: 6),
              Expanded(
                  child: Text(l.text,
                      style: TextStyle(fontSize: 12, color: Colors.grey.shade400))),
            ]),
          );
        }
        final you = l.role == 'you';
        final sys = l.role == 'sys';
        return Align(
          alignment: you ? Alignment.centerRight : Alignment.centerLeft,
          child: Container(
            margin: const EdgeInsets.symmetric(vertical: 4),
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
            constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.78),
            decoration: BoxDecoration(
              color: you
                  ? Colors.blue.shade600
                  : sys
                      ? Colors.grey.shade800
                      : const Color(0xFF1B2530),
              borderRadius: BorderRadius.circular(14),
            ),
            child: Text(l.text, style: const TextStyle(fontSize: 15, height: 1.3)),
          ),
        );
      },
    );
  }

  Widget _controls(bool needsSetup) {
    return SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
        child: Row(children: [
          // Hands-free toggle
          IconButton(
            tooltip: 'Hands-free',
            onPressed: needsSetup ? null : () => setState(() => _handsFree = !_handsFree),
            icon: Icon(_handsFree ? Icons.all_inclusive : Icons.repeat_one,
                color: _handsFree ? Colors.blueAccent : Colors.grey),
          ),
          const SizedBox(width: 8),
          // Big talk button (tap to toggle listen)
          Expanded(
            child: GestureDetector(
              onTap: needsSetup
                  ? null
                  : () => _listening ? _stopListening() : _startListening(),
              child: Container(
                height: 60,
                decoration: BoxDecoration(
                  color: _listening ? Colors.red.shade600 : Colors.blue.shade600,
                  borderRadius: BorderRadius.circular(30),
                ),
                child: Center(
                  child: Row(mainAxisSize: MainAxisSize.min, children: [
                    Icon(_listening ? Icons.stop : Icons.mic, color: Colors.white),
                    const SizedBox(width: 10),
                    Text(_listening ? 'Listening… tap to send' : 'Tap to talk',
                        style: const TextStyle(
                            color: Colors.white, fontSize: 16, fontWeight: FontWeight.w600)),
                  ]),
                ),
              ),
            ),
          ),
          const SizedBox(width: 8),
          // Type instead
          IconButton(
            tooltip: 'Type a message',
            onPressed: needsSetup ? null : _typeDialog,
            icon: const Icon(Icons.keyboard),
          ),
        ]),
      ),
    );
  }

  Future<void> _typeDialog() async {
    final ctrl = TextEditingController();
    final text = await showDialog<String>(
      context: context,
      builder: (_) => AlertDialog(
        title: const Text('Send to Otto'),
        content: TextField(
          controller: ctrl,
          autofocus: true,
          decoration: const InputDecoration(hintText: 'Type a command…'),
          onSubmitted: (v) => Navigator.pop(context, v),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(context, ctrl.text), child: const Text('Send')),
        ],
      ),
    );
    if (text != null && text.trim().isNotEmpty) _send(text);
  }

  Future<void> _openSettings() async {
    final hostCtrl = TextEditingController(text: _host);
    final tokenCtrl = TextEditingController(text: _token);
    bool tls = _tls;
    final saved = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setLocal) => AlertDialog(
          title: const Text('Connect to your Mac'),
          content: SingleChildScrollView(
            child: Column(mainAxisSize: MainAxisSize.min, children: [
              TextField(
                controller: hostCtrl,
                decoration: const InputDecoration(
                  labelText: 'Address',
                  hintText: '100.x.y.z:8000  or  192.168.1.20:8000',
                ),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: tokenCtrl,
                decoration: const InputDecoration(labelText: 'Token'),
              ),
              const SizedBox(height: 8),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Use TLS (wss://)'),
                subtitle: const Text('Off for Tailscale/LAN; on for a public https tunnel'),
                value: tls,
                onChanged: (v) => setLocal(() => tls = v),
              ),
            ]),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Save')),
          ],
        ),
      ),
    );
    if (saved == true) {
      setState(() {
        _host = hostCtrl.text.trim();
        _token = tokenCtrl.text.trim();
        _tls = tls;
      });
      await _save();
      _sub?.cancel();
      _ch?.sink.close(ws_status.normalClosure);
      _connected = false;
      _connect();
    }
  }
}
