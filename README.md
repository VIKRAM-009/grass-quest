# 🌿 Grass Quest

A daily outdoor mission that gets you off your screen. It runs on open-weight AI models: on your own laptop, or with a little help from Ollama Cloud.

1. It checks today's real weather for your city ([Open-Meteo](https://open-meteo.com), free and needs no key).
2. A local **Llama 3.2** writes a short outdoor mission that fits the weather.
3. You put the phone away and go outside.
4. You come back with up to 3 photos, and **Gemma 4** (an open-weight vision model, on Ollama Cloud) checks them and gives points. Want it fully offline? Use a local vision model like **Qwen2.5-VL** instead (see below).
5. Points and a daily streak keep you coming back... to go outside again.

## Run it

```bash
ollama pull llama3.2
ollama signin                 # free Ollama account, for the cloud vision model
ollama pull gemma4:31b-cloud
pip install flask pillow
python3 app.py
```

Open `http://localhost:5000` on the laptop, or `http://<your-laptop-ip>:5000` on your phone when it's on the same WiFi. That way you can upload photos straight from the camera.

Use different models with environment variables:

```bash
TEXT_MODEL=qwen2.5 VISION_MODEL=gemma3:4b python3 app.py
```

Fully offline (needs about 10 GB of free RAM, and each photo takes about 45 seconds on a CPU):

```bash
ollama pull qwen2.5vl:3b
VISION_MODEL=qwen2.5vl:3b python3 app.py
```

## Privacy

Your location and history stay on your machine. By default, your photos are sent to Ollama Cloud so Gemma 4 can check them. If you want nothing to leave your laptop except the weather lookup, use a local vision model (see above).
