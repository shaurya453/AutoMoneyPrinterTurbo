# Kokoro TTS assets

Place the Kokoro model and voice metadata files in this directory:

- `kokoro-v1.0.onnx` (required)
- `voices-v1.0.bin` (required)

Download commands (already used during setup):

```bash
curl -L -o kokoro-v1.0.onnx https://github.com/nazdridoy/kokoro-tts/releases/download/v1.0.0/kokoro-v1.0.onnx
curl -L -o voices-v1.0.bin https://github.com/nazdridoy/kokoro-tts/releases/download/v1.0.0/voices-v1.0.bin
```

The pipeline uses the `kokoro-venv` virtual environment (Python 3.12) and runs `kokoro-tts` via subprocess, so these files just need to exist at runtime; they are ignored from git via `.gitignore`.
