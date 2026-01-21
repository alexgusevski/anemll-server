# Anemll Server

This is an OpenAI-compatible API server for [Anemll](https://github.com/Anemll/Anemll) models. It provides a `/v1/chat/completions` endpoint that follows the OpenAI API format. It also provides the `/v1/models` endpoint which allows it to work with Open WebUI.

## Features

- OpenAI-compatible API
- Streaming responses
- System prompt, conversation history supported
- Works with Open WebUI (other frontends not tested but should work aswell)

A version of `chat_full.py` (from the [swift-inference](https://github.com/Anemll/Anemll/blob/swift-inference/tests/chat_full.py) branch, which runs the fastest for me) is included for ease of use.

## Installation

1. Install the required dependencies, preferably in a conda or venv environment


```bash
pip install -r requirements.txt
```


2. You will also need to download an Anemll model. I have used [this one](https://huggingface.co/anemll/anemll-Meta-Llama-3.2-1B-ctx2048_0.1.2) from the official Anemll Huggingface, 0.1.1 should also work fine.




## Configuration


Modify the `MODEL_DIR` variable in `server.py` to your Anemll model path. 


```python
# Hardcoded model directory path
MODEL_DIR = "/example-path/anemll-Meta-Llama-3.2-1B-ctx2048_0.1.2"
```

## Usage

Run the server with:

```bash
python server.py
```

The server will start on `0.0.0.0:8000` by default.

In order to connect Open WebUI to it, simply go to "Connections" in the settings and enter this as the base URL: `http://0.0.0.0:8000/v1`.

## Known issues, limitations

Sometimes, but rarely, when you start the server you will get a GIL issue when you try to generate a response. Just restart the server and it will most likely work the next time you run it, and keep working from then on.


## One last thing

Anemll is still in its early stages, with a limited amount of models on Hugging Face and development of the core library still ongoing. This presents a unique opportunity to become an early contributor to this emerging technology. Whether you're interested in experimenting with the library, converting models, contributing code, or simply raising awareness - your involvement can help shape the future of on-device AI acceleration. The ANE represents a significant advancement in efficient ML inference, and community participation is vital to realizing its full potential.

## API Endpoints

### `/v1/chat/completions`

This endpoint follows the OpenAI API format for chat completions.

Example request:

```json
{
  "model": "anemll-model",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello, how are you?"}
  ],
  "temperature": 0.7,
  "stream": true
}
```

### `/v1/models`

Lists available models. Needed to work with Open WebUI.


## Testing with curl

### Non-streaming request:

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "anemll-model",
    "messages": [
      {"role": "system", "content": "Whatever you do, always reply in ALL CAPS!"},
      {"role": "user", "content": "Hello, how are you?"}
    ],
    "temperature": 0.7,
    "stream": false
  }'
```

### Streaming request:

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "anemll-model",
    "messages": [
      {"role": "system", "content": "Whatever you do, always reply in ALL CAPS!"},
      {"role": "user", "content": "Hello, how are you?"}
    ],
    "temperature": 0.7,
    "stream": true
  }'
```

### List models:

```bash
curl http://localhost:8000/v1/models
```


## Links

Unofficial Discord server for Anemll:

https://discord.gg/xgtQDDBGcM


If you're interested in a general AI system performing any kind of digital labour for you, visit: 

https://planetarylabour.com

## License

MIT - Do whatever you want with this.
