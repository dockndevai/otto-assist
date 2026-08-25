# Reliable hands-free "hey otto" (trained openWakeWord model)

The most reliable wake word is a **trained acoustic model** (how Alexa/Siri work) — it
detects "hey otto" from the sound, independent of speech-to-text. It's free, but training
needs a GPU, and **it can't be trained on macOS** (openWakeWord's sample generator is
Linux-only). So training happens in **Google Colab** (free GPU, your Google account).

> ⚠️ The *official* openWakeWord training notebook is **broken as of 2026** (Python 3.12 /
> package changes). Use the community "2026 edition" notebook below, which is kept working.

## 1. Train the model (Colab, ~75–90 min, hands-off)

1. Open the maintained trainer notebook:
   **https://github.com/alfiedennen/openwakeword-colab-2026** → open its notebook in Colab
   (the repo's README has an "Open in Colab" button).
2. In Colab: **Runtime → Change runtime type → GPU → Save**.
3. Set the wake phrase where the notebook asks (e.g. `target_word = "hey otto"`).
4. **Runtime → Run all**, then leave it — it generates samples, trains, and exports a model
   (`.tflite` and/or `.onnx`). ~75–90 min.
5. **Download** the resulting `hey_otto.onnx` (or `.tflite`) to your Mac.

## 2. Install it into Otto (one command)

```bash
bash scripts/install_wake_word.sh ~/Downloads/hey_otto.onnx
```

Copies the model into `~/Library/Application Support/Otto/`, switches the config to the
`oww` backend pointing at it, and restarts Otto. Then say **"hey otto"**.

## Don't want to spend 90 minutes?

Two immediate, reliable alternatives (no training):

- **A built-in trained phrase.** openWakeWord ships dependable models for `hey jarvis`,
  `alexa`, and `hey mycroft`. Set `voice.stt.wakeword_backend: oww` and
  `voice.stt.wake_words: "hey jarvis"` (no model file needed). Reliable now — just not the
  word "otto".
- **The 🎤 Talk button** — click and speak, zero setup, 100% reliable.

## Tuning

After installing, adjust in Settings / config: `voice.stt.wake_word_sensitivity` (higher =
more sensitive, more false triggers).
