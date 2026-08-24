# Reliable hands-free "hey otto" (trained openWakeWord model)

The software wake word rides on speech-to-text, which drops the name "Otto" too often. A
**trained openWakeWord model** detects "hey otto" *acoustically* (like Alexa/Siri do) — it
never depends on Whisper hearing the name, so it's reliable and low-power. It's free; the
only step Otto can't do for you is the training run, which needs a Google account + a GPU
(both free via Google Colab, ~20 minutes).

## 1. Train the model (Google Colab, ~20 min)

1. Open openWakeWord's **automatic model training** notebook in Colab:
   https://github.com/dscripka/openWakeWord → `notebooks/automatic_model_training.ipynb`
   → click **"Open in Colab"**. (Direct: search "openWakeWord automatic model training
   colab".)
2. In Colab: **Runtime → Change runtime type → GPU → Save**.
3. Find the config cell and set the target phrase:
   ```python
   target_word = "hey otto"
   ```
   (Leave the other defaults; more training steps = better accuracy but longer.)
4. **Runtime → Run all.** It synthesizes many "hey otto" samples, mixes in background
   noise, and trains. When done it produces **`hey_otto.onnx`** (and usually
   `hey_otto.tflite`).
5. Download `hey_otto.onnx` to your Mac (Colab file browser → download).

## 2. Install it into Otto (one command)

```bash
bash scripts/install_wake_word.sh ~/Downloads/hey_otto.onnx
```

That copies the model into `~/Library/Application Support/Otto/`, switches the live config to
`wakeword_backend: oww` pointing at it, and restarts the service.

## 3. Use it

Say **"hey otto"** — the model wakes Otto acoustically, then Whisper transcribes just your
command (which it already does reliably). Tune detection in the Settings UI or config:
`voice.stt.wake_word_sensitivity` (higher = more sensitive, more false triggers).

## Fallbacks

- The **🎤 Talk** button / ⌘⇧J always works and needs no model — use it any time.
- Prefer not to train? Keep `wakeword_backend: phrase` (convenient, less reliable) or use a
  built-in openWakeWord phrase like `hey jarvis` (`backend: oww`, no model file).
