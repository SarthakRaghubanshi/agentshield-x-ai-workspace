# Setup guide: Windows, macOS, Linux

There are two ways to run the AI Workspace:

| Option | When to use | What you install |
|---|---|---|
| **A. Local Python** | development, quick experiments, debugging | Python 3.12/3.13, git, a model |
| **B. Docker** | demos, evaluation runs, the isolated sandbox (PRD NFR-7, FR-11) | Docker, git, a model |

Either way you need **at least one model**. Pick one or more:

* **Ollama** (free, local, simplest): `qwen2.5:7b` is the recommended default because it handles tool calls reliably. `llama3.1:8b` also works. Small models (≤3B) often call tools badly (PRD risk table).
* **vLLM / llama.cpp** (free, local, for the college GPU server)
* **Cloud API** (optional, paid, capped at USD 2 per process): Gemini, OpenAI or Anthropic

> Use **Python 3.12 or 3.13**. Python 3.14 is too new for some dependencies (faiss-cpu, tokenizers) on some platforms.

---

## Windows 10 / 11

### 1. Prerequisites

Open **PowerShell** and run:

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
winget install -e --id Ollama.Ollama          # optional: local models
winget install -e --id Docker.DockerDesktop   # optional: option B (needs WSL2, reboot)
```

Close and reopen PowerShell afterwards so the new commands are on `PATH`.

### 2. Get the code

```powershell
git clone https://github.com/Kroszborg/agentshield-x-ai-workspace.git
cd agentshield-x-ai-workspace
```

### 3A. Run with local Python

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If activation fails with *"running scripts is disabled on this system"*, run this once and try again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

(or use `cmd.exe` and `.venv\Scripts\activate.bat`).

Get a model (Ollama runs in the background after installation; check the tray icon):

```powershell
ollama pull qwen2.5:7b
```

Optional cloud keys:

```powershell
copy .env.example .env
notepad .env        # fill in GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY
```

Start the console:

```powershell
python -m aiworkspace serve
```

Open <http://localhost:8000>.

### 3B. Run with Docker Desktop

Start Docker Desktop and wait until it says *Engine running*. Then:

```powershell
copy .env.example .env     # optional, only for cloud keys
docker compose up --build
```

Open <http://localhost:8000>. Ollama installed on Windows is reached automatically at
`host.docker.internal:11434`. Stop with `Ctrl+C` or `docker compose down`.

---

## macOS (Apple Silicon and Intel)

### 1. Prerequisites

Install [Homebrew](https://brew.sh) if you do not have it, then:

```bash
brew install python@3.12 git
brew install ollama                 # optional: local models (or the app from ollama.com)
brew install --cask docker          # optional: option B (Docker Desktop)
```

### 2. Get the code

```bash
git clone https://github.com/Kroszborg/agentshield-x-ai-workspace.git
cd agentshield-x-ai-workspace
```

### 3A. Run with local Python

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Start Ollama and get a model. On Apple Silicon Ollama uses the GPU (Metal) automatically.

```bash
brew services start ollama          # or open the Ollama app
ollama pull qwen2.5:7b
```

Optional cloud keys:

```bash
cp .env.example .env
open -e .env                        # fill in the keys you want
```

Start the console:

```bash
python -m aiworkspace serve
```

Open <http://localhost:8000>.

### 3B. Run with Docker Desktop

```bash
cp .env.example .env                # optional, only for cloud keys
docker compose up --build
```

Docker containers on macOS cannot use the Mac's GPU. Run Ollama **natively** (step 3A) rather
than in the `ollama` container. The workspace container reaches it at
`host.docker.internal:11434` without extra configuration.

---

## Linux (Ubuntu / Debian; Fedora notes inline)

### 1. Prerequisites

```bash
# Ubuntu 24.04 ships Python 3.12
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl
# Fedora:  sudo dnf install -y python3.12 git curl

# Optional: Ollama (installs a systemd service)
curl -fsSL https://ollama.com/install.sh | sh

# Optional: Docker Engine + compose plugin (option B)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER       # then log out and back in
```

Check `python3 --version`. If it is older than 3.12, install 3.12 from your distribution or
the deadsnakes PPA (`sudo apt install python3.12 python3.12-venv`) and use `python3.12` below.

### 2. Get the code

```bash
git clone https://github.com/Kroszborg/agentshield-x-ai-workspace.git
cd agentshield-x-ai-workspace
```

### 3A. Run with local Python

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
ollama pull qwen2.5:7b
cp .env.example .env                # optional, for cloud keys
python -m aiworkspace serve
```

Open <http://localhost:8000>. On a headless server use
`python -m aiworkspace serve --host 0.0.0.0` and browse to `http://<server-ip>:8000`.
The console has no login, so only do this on a trusted network.

### 3B. Run with Docker

```bash
docker compose up --build
```

**Using Ollama installed on the host:** by default Ollama on Linux only listens on 127.0.0.1,
so containers cannot reach it. Make it listen on all interfaces:

```bash
sudo systemctl edit ollama
# add these two lines, save, exit:
# [Service]
# Environment="OLLAMA_HOST=0.0.0.0"
sudo systemctl restart ollama
```

**Or use the bundled Ollama container** (models are stored in a Docker volume):

```bash
echo "DOCKER_OLLAMA_API_BASE=http://ollama:11434" >> .env
docker compose --profile ollama up --build -d
docker compose exec ollama ollama pull qwen2.5:7b
```

**With an NVIDIA GPU** (install the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) first):

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile ollama up --build -d
```

---

## Other local model servers (college GPU)

Both expose an OpenAI-compatible API, and both are already registered in `config/models.yaml`.

**vLLM** (Linux + NVIDIA). Tool calling needs the two `--tool-call` flags:

```bash
pip install vllm
vllm serve Qwen/Qwen2.5-7B-Instruct --port 8001 --enable-auto-tool-choice --tool-call-parser hermes
```

Set `VLLM_API_BASE=http://<host>:8001/v1` and `VLLM_MODEL=Qwen/Qwen2.5-7B-Instruct` in `.env`,
then choose **vLLM** in the console.

**llama.cpp server** (any OS). `--jinja` enables tool calling:

```bash
llama-server -m qwen2.5-7b-instruct-q4_k_m.gguf --port 8080 --jinja
```

Set `LLAMACPP_API_BASE=http://<host>:8080/v1` in `.env`, then choose **llama.cpp server**.

When the workspace runs in Docker, put the same URLs in `DOCKER_VLLM_API_BASE` /
`DOCKER_LLAMACPP_API_BASE` (use the machine's IP or `host.docker.internal`, never `localhost`).

## Cloud API keys

| Provider | Variable in `.env` | Get a key |
|---|---|---|
| Google Gemini (has a free tier) | `GEMINI_API_KEY` | <https://aistudio.google.com/apikey> |
| OpenAI | `OPENAI_API_KEY` | <https://platform.openai.com/api-keys> |
| Anthropic | `ANTHROPIC_API_KEY` | <https://console.anthropic.com/settings/keys> |

Restart the server after editing `.env`. Total paid spend per process is capped by
`budget.max_usd` in `config/workspace.yaml` (default USD 2).

---

## Check the installation

With the virtual environment active:

```bash
python -m aiworkspace tasks     # lists 16 tasks (9 benign, 7 attack)
pytest -q                       # 16 passed, 2 skipped (the skipped ones need a real model)
```

Then open <http://localhost:8000>. The header should read `sandbox: local · 16 tools`
(`sandbox: remote` under Docker), and your model should be selectable (not greyed out) in the
**Model** dropdown. Run the task **Summarise the quarterly report** as a first test.

Optional real-model test (PRD acceptance criterion 1):

```bash
# macOS / Linux
AIWORKSPACE_LIVE_MODELS=ollama-qwen2.5,gemini-flash pytest -q tests/test_live_model.py -s
```
```powershell
# Windows PowerShell
$env:AIWORKSPACE_LIVE_MODELS="ollama-qwen2.5,gemini-flash"; pytest -q tests/test_live_model.py -s
```

## Updating

```bash
git pull
pip install -r requirements.txt          # local option
docker compose up --build                # Docker option
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Model greyed out: *endpoint … not reachable or model not pulled* | Start Ollama (`ollama serve` / the app / `systemctl start ollama`) and run `ollama pull qwen2.5:7b`. Then reload the page. |
| Model greyed out: *set GEMINI_API_KEY* | Put the key in `.env` and restart the server. |
| `pip install` fails building faiss / tokenizers | You are probably on Python 3.14. Create the venv with 3.12 or 3.13. |
| PowerShell: *running scripts is disabled* | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `address already in use` on port 8000 | `python -m aiworkspace serve --port 8080`, or in Docker change `"8000:8000"` to `"8080:8000"`. |
| Docker on Linux: *permission denied … docker.sock* | `sudo usermod -aG docker $USER`, then log out and back in. |
| Docker: model unreachable from the container | Use `host.docker.internal` or `http://ollama:11434` in the `DOCKER_*` variables, never `localhost`. On Linux, set `OLLAMA_HOST=0.0.0.0` (see above). |
| Runs end with *model error … tool* | The model does not support tool calling. Use `qwen2.5:7b`, `llama3.1:8b` or a cloud model; for vLLM add the tool-call flags. |
| Runs end with *maximum number of steps reached* | The model looped. Raise `agent.max_steps` in `config/workspace.yaml` or use a stronger model. |
| *BudgetExceededError* | The paid-API cap was reached. Raise `budget.max_usd` deliberately, or restart the process. |
| Start from scratch | Stop the server, delete the `runtime/` folder (local) or run `docker compose down -v` (Docker). |
